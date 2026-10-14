"""HU015 y HU019 en el canal web: crear, publicar y consultar servicios propios.

Contratos de ADR-027 (`POST /v1/services` y `POST /v1/services/{service_id}/publish`) y
de ADR-038 (`GET /v1/me/services` y `GET /v1/me/services/{service_id}`). El BFF
autentica, aplica el filtro de borde (permisos `services.create` y `services.publish`,
o actor `service_provider` en las consultas) y reenvía el cuerpo, la clave de
idempotencia y los parámetros sin reinterpretarlos: las reglas de la oferta, la
propiedad y los errores `422 validation_error` con `details[{field, issue}]` los decide
el servicio propietario y se entregan sin alterar. Los instantes llegan en UTC (`Z`); la
conversión a la zona del usuario corresponde al frontend.
"""

from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from bff_web.api.training import (
    _UNAUTHORIZED,
    _UPSTREAM,
    SERVICE_PROVIDER,
    _passthrough,
    _permission_required,
    _Session,
    _upstream_failure,
    authenticate_session,
)
from bff_web.clients.complementary_services import ComplementaryServicesClient
from bff_web.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.authorization import ActorPermissions
from sportapp_platform.errors import ErrorResponse
from sportapp_platform.tokens import VerifiedAccessToken

SOURCE = "complementary-services"
CREATE_PERMISSION = "services.create"
PUBLISH_PERMISSION = "services.publish"
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"

Category = Literal["SPORTS_MEDICINE", "COACHING", "NUTRITION", "TRANSPORT", "ACCOMPANIMENT"]
Modality = Literal["IN_PERSON", "VIRTUAL", "HYBRID"]
BillingUnit = Literal["SERVICE", "SESSION", "HOUR", "DAY"]


class ServicePrice(BaseModel):
    """Precio público de referencia en COP, impuestos aplicables incluidos; no es un cobro."""

    model_config = ConfigDict(extra="forbid")

    amount: Annotated[
        str,
        Field(
            pattern=r"^\d{1,9}(\.\d{1,2})?$",
            description="Cadena decimal de 0.00 a 999999999.99, máximo dos decimales.",
            examples=["120000.00"],
        ),
    ]
    currency: Literal["COP"]
    billing_unit: BillingUnit


class AvailabilityInterval(BaseModel):
    """Intervalo `[starts_at, ends_at)`. En la solicitud, con desfase coherente con
    `time_zone`; en la respuesta, en UTC con sufijo `Z`."""

    model_config = ConfigDict(extra="forbid")

    starts_at: datetime
    ends_at: datetime
    time_zone: Annotated[str, Field(description="Zona IANA declarada, p. ej. America/Bogota.")]
    capacity: Annotated[int, Field(ge=1, le=100)]


class CreateServiceRequest(BaseModel):
    """Esquema documentado del cuerpo; la validación la hace el servicio (ADR-027)."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[
        str, Field(min_length=3, max_length=120, description="Se recortan los espacios exteriores.")
    ]
    description: Annotated[
        str,
        Field(min_length=20, max_length=2000, description="Se recortan los espacios exteriores."),
    ]
    category: Category
    modality: Modality
    price: ServicePrice
    availability: Annotated[
        list[AvailabilityInterval] | None,
        Field(
            max_length=100,
            description="Opcional; intervalos futuros de hasta 24 h, con fin dentro de 365 días, "
            "contiguos permitidos y sin superposición.",
        ),
    ] = None


class ServiceResponse(BaseModel):
    service_id: UUID
    provider_id: UUID
    name: str
    description: str
    category: Category
    modality: Modality
    price: ServicePrice
    availability: list[AvailabilityInterval]
    status: Literal["DRAFT", "PUBLISHED"]
    version: int
    created_at: datetime
    updated_at: datetime
    published_at: Annotated[
        datetime | None, Field(description="UTC (`Z`); `null` mientras está en borrador.")
    ]


class PublishServiceResponse(BaseModel):
    service_id: UUID
    provider_id: UUID
    status: Literal["PUBLISHED"]
    version: int
    published_at: datetime
    updated_at: datetime


class MyServicesResponse(BaseModel):
    items: list[ServiceResponse]
    next_cursor: str | None


_REPLAYED_HEADER_DOC = {
    REPLAYED_HEADER: {
        "description": "`true` cuando se devuelve el resultado de un reintento.",
        "schema": {"type": "string"},
    }
}
_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    HTTPStatus.NOT_FOUND.value: {
        "model": ErrorResponse,
        "description": "`service_not_found`: no existe o es de otro profesional.",
    }
}
_MINE_FORBIDDEN: dict[int | str, dict[str, Any]] = {
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required`: solo el actor `service_provider` consulta.",
    }
}


def _request_schema() -> dict[str, Any]:
    schema = CreateServiceRequest.model_json_schema(ref_template="#/components/schemas/{model}")
    schema.pop("$defs", None)
    return schema


def build_services_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    permissions: ActorPermissions,
    services: ComplementaryServicesClient,
    channel: str,
) -> APIRouter:
    router = APIRouter(tags=["services"])

    def authorize(authorization: str | None, permission: str) -> _Session | Response:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        actor = session.actor_type
        if actor is None or permission not in permissions.for_actor(actor, channel):
            return _permission_required()
        return session

    def own_services_session(authorization: str | None) -> _Session | Response:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        if session.actor_type != SERVICE_PROVIDER:
            return _permission_required()
        return session

    def private(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.post(
        "/v1/services",
        status_code=HTTPStatus.CREATED,
        response_model=ServiceResponse,
        summary="Crear un servicio en borrador (HU015)",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": _request_schema()}},
            }
        },
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            HTTPStatus.CREATED.value: {
                "headers": {
                    "Location": {"schema": {"type": "string"}},
                    REPLAYED_HEADER: {
                        "description": "`true` cuando se devuelve el resultado de un reintento.",
                        "schema": {"type": "string"},
                    },
                }
            },
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` (`services.create`).",
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`idempotency_key_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` con `details[{field, issue}]` (ADR-027).",
            },
        },
    )
    def create(
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = authorize(authorization, CREATE_PERMISSION)
        if isinstance(session, Response):
            return session
        try:
            upstream = services.create_service(session.token, body, idempotency_key)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        return _passthrough(
            upstream,
            {
                HTTPStatus.CREATED,
                HTTPStatus.FORBIDDEN,
                HTTPStatus.CONFLICT,
                HTTPStatus.UNPROCESSABLE_ENTITY,
            },
            headers=("Location", REPLAYED_HEADER),
            source=SOURCE,
        )

    @router.post(
        "/v1/services/{service_id}/publish",
        response_model=PublishServiceResponse,
        summary="Publicar un servicio propio en borrador (HU019)",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            **_NOT_FOUND,
            HTTPStatus.OK.value: {
                "description": "Servicio publicado, o estado vigente si ya lo estaba.",
                "headers": _REPLAYED_HEADER_DOC,
            },
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` (`services.publish`).",
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`idempotency_key_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` con `details[{field, issue}]` (ADR-027): "
                "clave de idempotencia ausente o inválida, u oferta que ya no cumple las "
                "reglas (p. ej. `availability` `required` sin intervalos futuros).",
            },
        },
    )
    def publish(
        service_id: UUID,
        idempotency_key: Annotated[
            str | None,
            Header(
                alias=IDEMPOTENCY_HEADER, description="UUID; obligatorio (lo valida el servicio)."
            ),
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = authorize(authorization, PUBLISH_PERMISSION)
        if isinstance(session, Response):
            return session
        try:
            upstream = services.publish_service(session.token, service_id, idempotency_key)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        return _passthrough(
            upstream,
            {
                HTTPStatus.OK,
                HTTPStatus.UNAUTHORIZED,
                HTTPStatus.FORBIDDEN,
                HTTPStatus.NOT_FOUND,
                HTTPStatus.CONFLICT,
                HTTPStatus.UNPROCESSABLE_ENTITY,
            },
            headers=(REPLAYED_HEADER,),
            source=SOURCE,
        )

    @router.get(
        "/v1/me/services",
        response_model=MyServicesResponse,
        summary="Mis servicios, del más reciente al más antiguo (HU019, ADR-038)",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            **_MINE_FORBIDDEN,
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` en `query.status`, `query.limit` o "
                "`query.cursor`.",
            },
        },
    )
    def list_mine(
        *,
        status: Annotated[
            str | None, Query(description="`DRAFT` o `PUBLISHED`; sin filtro si se omite.")
        ] = None,
        limit: Annotated[
            str | None, Query(description="Entero de 1 a 100; 20 por defecto.")
        ] = None,
        cursor: Annotated[
            str | None, Query(description="`next_cursor` de la página anterior.")
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = own_services_session(authorization)
        if isinstance(session, Response):
            return session
        # Solo los presentes y tal como llegan: el servicio valida y responde el `422`.
        pairs = (("status", status), ("limit", limit), ("cursor", cursor))
        params = {name: value for name, value in pairs if value is not None}
        try:
            upstream = services.list_my_services(session.token, params)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        return private(
            _passthrough(
                upstream,
                {
                    HTTPStatus.OK,
                    HTTPStatus.UNAUTHORIZED,
                    HTTPStatus.FORBIDDEN,
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                },
                source=SOURCE,
            )
        )

    @router.get(
        "/v1/me/services/{service_id}",
        response_model=ServiceResponse,
        summary="Detalle de un servicio propio (HU019, ADR-038)",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            **_MINE_FORBIDDEN,
            **_NOT_FOUND,
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `path.service_id` no es un UUID.",
            },
        },
    )
    def get_mine(
        service_id: UUID,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = own_services_session(authorization)
        if isinstance(session, Response):
            return session
        try:
            upstream = services.get_my_service(session.token, service_id)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        return private(
            _passthrough(
                upstream,
                {
                    HTTPStatus.OK,
                    HTTPStatus.UNAUTHORIZED,
                    HTTPStatus.FORBIDDEN,
                    HTTPStatus.NOT_FOUND,
                },
                source=SOURCE,
            )
        )

    return router
