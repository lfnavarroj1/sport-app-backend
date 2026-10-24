"""HU002 en el canal web: datos de la cuenta propia de cualquier actor (ADR-035).

El BFF autentica (cualquier actor: `athlete`, `organizer` y `service_provider`; no hay filtro
de capacidad ni `403`) y reenvía el cuerpo, la clave de idempotencia y el token sin
reinterpretarlos: la normalización y unicidad del correo, la concurrencia por
`expected_version`, la idempotencia y los errores `422 validation_error` con
`details[{field, issue}]` los decide `users_management` y se entregan sin alterar. Nombre y
correo son datos personales: nunca se registran y ninguna respuesta se guarda en caché
(`no-store`). Los instantes llegan en UTC (`Z`); la conversión corresponde al frontend.
"""

from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from starlette.concurrency import run_in_threadpool

from bff_web.api.profile import (
    CACHE_CONTROL_HEADER,
    IDEMPOTENCY_HEADER,
    REPLAYED_HEADER,
    SOURCE,
    UTC_INSTANT,
)
from bff_web.api.training import (
    _UNAUTHORIZED,
    _UPSTREAM,
    _passthrough,
    _upstream_failure,
    authenticate_session,
)
from bff_web.clients.users_management import (
    UpstreamTimeout,
    UpstreamUnavailable,
    UsersManagementClient,
)
from sportapp_platform.errors import ErrorResponse
from sportapp_platform.tokens import VerifiedAccessToken

ActorType = Literal["athlete", "organizer", "service_provider"]
AccountStatus = Literal["active", "disabled"]


class AccountUpdateRequest(BaseModel):
    """Esquema documentado del cuerpo; la validación la hace el servicio (ADR-035).

    `user_id`, `actor_type`, `status`, `password` o cualquier otro campo →
    `422 field_not_allowed`: solo se edita la cuenta del token.
    """

    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[
        int, Field(ge=1, description="Obligatorio: `version` vigente de la cuenta.")
    ]
    full_name: Annotated[
        str,
        Field(
            min_length=1,
            max_length=120,
            description="Se recortan los espacios exteriores y se colapsan los internos.",
        ),
    ]
    email: Annotated[
        EmailStr,
        Field(
            max_length=254,
            description="Se normaliza a minúsculas. Igual al actual no es error ni crea "
            "versión; si pertenece a otra cuenta → `409 email_already_registered`.",
        ),
    ]


class AccountResponse(BaseModel):
    """Cuenta confirmada después del `COMMIT` (no espera la proyección)."""

    user_id: UUID
    full_name: str
    email: str
    actor_type: ActorType
    status: AccountStatus
    version: int
    updated_at: Annotated[datetime, Field(description=UTC_INSTANT)]


class AccountReadResponse(BaseModel):
    """Cuenta proyectada (consistencia eventual): tras un comando, si `version` es menor que
    la confirmada, el cliente conserva la respuesta del comando y reintenta la consulta."""

    user_id: UUID
    full_name: str
    email: str
    actor_type: ActorType
    status: AccountStatus
    version: int
    projected_at: Annotated[
        datetime, Field(description="Última proyección aplicada, en UTC (`Z`).")
    ]


_CACHE_HEADER_DOC = {
    CACHE_CONTROL_HEADER: {"description": "`no-store`.", "schema": {"type": "string"}}
}


def build_account_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    users: UsersManagementClient,
) -> APIRouter:
    router = APIRouter(tags=["account"])

    def private(response: Response) -> Response:
        response.headers[CACHE_CONTROL_HEADER] = "no-store"
        return response

    def forward(
        send: Callable[[str], Any], authorization: str | None, allowed: set[int]
    ) -> Response:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return private(session)
        try:
            upstream = send(session.token)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return private(_upstream_failure(error))
        return private(
            _passthrough(
                upstream, allowed, headers=(REPLAYED_HEADER, CACHE_CONTROL_HEADER), source=SOURCE
            )
        )

    @router.put(
        "/v1/me/account",
        response_model=AccountResponse,
        summary="Editar mi nombre y correo (HU002, ADR-035)",
        description="Cualquier actor. El cambio de correo no revoca la sesión ni exige "
        "verificación en el MVP: la sesión vigente sigue renovándose y el siguiente inicio de "
        "sesión usa el correo nuevo.",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": AccountUpdateRequest.model_json_schema()}
                },
            }
        },
        responses={
            HTTPStatus.OK.value: {
                "description": "Cuenta confirmada (o reintento con la misma clave).",
                "headers": {
                    REPLAYED_HEADER: {
                        "description": "`true` cuando se devuelve el resultado de un reintento.",
                        "schema": {"type": "string"},
                    },
                    **_CACHE_HEADER_DOC,
                },
            },
            **_UNAUTHORIZED,
            **_UPSTREAM,
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": '`version_conflict` con `details=[{field: "expected_version", '
                'issue: "<versión vigente>"}]`; `email_already_registered` (sin revelar nada '
                "de la otra cuenta) o `idempotency_key_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` con `details[{field, issue}]` (catálogo de "
                "ADR-034): `required`, `invalid_length`, `unknown_value`, `out_of_range`, "
                "`field_not_allowed`; incluye `Idempotency-Key` ausente (`required`) o que no "
                "es un UUID (`unknown_value`). Todo o nada.",
            },
        },
    )
    async def put_account(
        request: Request,
        idempotency_key: Annotated[
            str | None,
            Header(
                alias=IDEMPOTENCY_HEADER, description="UUID; obligatorio (lo valida el servicio)."
            ),
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        # Bytes crudos: el BFF no interpreta el JSON y autentica antes de cualquier validación.
        body = await request.body()
        return await run_in_threadpool(
            forward,
            lambda token: users.put_account(token, body, idempotency_key),
            authorization,
            {
                HTTPStatus.OK,
                HTTPStatus.UNAUTHORIZED,
                HTTPStatus.CONFLICT,
                HTTPStatus.UNPROCESSABLE_ENTITY,
            },
        )

    @router.get(
        "/v1/me/account",
        response_model=AccountReadResponse,
        summary="Mi cuenta (HU002, ADR-035)",
        description="Consulta la proyección `user_directory`: es eventual.",
        responses={
            HTTPStatus.OK.value: {"headers": _CACHE_HEADER_DOC},
            **_UNAUTHORIZED,
            **_UPSTREAM,
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": "`user_not_found`: la cuenta aún no está proyectada; reintentar.",
            },
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": "`upstream_unavailable`: el servicio o su base de lectura no "
                "están disponibles.",
            },
        },
    )
    def get_account(authorization: Annotated[str | None, Header()] = None) -> Response:
        return forward(
            users.get_account,
            authorization,
            {
                HTTPStatus.OK,
                HTTPStatus.UNAUTHORIZED,
                HTTPStatus.NOT_FOUND,
                HTTPStatus.SERVICE_UNAVAILABLE,
            },
        )

    return router
