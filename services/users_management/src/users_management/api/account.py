"""HU002: datos de la cuenta propia para cualquier actor (contrato de ADR-035).

- `PUT /v1/me/account`: edita `full_name` y `email` con `expected_version` obligatorio e
  `Idempotency-Key`; responde `200` con la cuenta confirmada (incluso si nada cambió).
- `GET /v1/me/account`: lee la proyección `user_directory` (consulta eventual).

El cuerpo del `PUT` se lee como JSON sin validación automática para autenticar primero y
responder con el catálogo de ADR-034 (`details[{field, issue}]`); su esquema se documenta en
el OpenAPI. `user_id`, `actor_type`, `status`, `password` o cualquier otro campo →
`field_not_allowed` (CA2): no existe una ruta para cuentas ajenas. Nombre y correo son datos
personales: todas las respuestas llevan `Cache-Control: no-store` y nunca se registran.
"""

from datetime import UTC, datetime
from http import HTTPStatus
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field, TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool

from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from users_management.api.identity import (
    TokenVerifier,
    authenticate_user,
    correlation_uuid,
    unauthenticated,
)
from users_management.api.lookup import USER_NOT_FOUND
from users_management.api.profile import (
    IDEMPOTENCY_HEADER,
    INSTANT_DESCRIPTION,
    NO_STORE,
    REPLAYED_HEADER,
    decode_body,
    parse_idempotency_key,
    validation_failure,
)
from users_management.application.commands.update_account import (
    UpdateAccount,
    UpdateAccountCommand,
)
from users_management.application.errors import (
    AccountNotFound,
    IdempotencyKeyReused,
    ReadModelUnavailable,
)
from users_management.application.queries.get_account import GetAccount
from users_management.domain.account.editing import AccountChangeInput
from users_management.domain.account.errors import AccountVersionConflict, EmailAlreadyRegistered
from users_management.domain.account.model import (
    EMAIL_MAX_LENGTH,
    FULL_NAME_MAX_LENGTH,
    AccountStatus,
    ActorType,
)
from users_management.domain.profile.validation import InvalidProfileSection, Issue, Violation

ACCOUNT_FIELDS = ("expected_version", "full_name", "email")

# El mismo validador de formato que el registro (HU001), para que la unicidad compare igual.
_EMAIL = TypeAdapter(EmailStr)


class AccountUpdateRequest(BaseModel):
    """Nombre y correo completos de la cuenta propia (HU002, ADR-035)."""

    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[
        int, Field(ge=1, description="Versión vigente de la cuenta (`version` de la lectura).")
    ]
    full_name: Annotated[
        str,
        Field(
            min_length=1,
            max_length=FULL_NAME_MAX_LENGTH,
            description="Se recortan los espacios y se colapsan los internos.",
        ),
    ]
    email: Annotated[
        EmailStr,
        Field(
            max_length=EMAIL_MAX_LENGTH,
            description="Se normaliza a minúsculas. Igual al actual no es error ni crea "
            "versión; si pertenece a otra cuenta → `409 email_already_registered`.",
        ),
    ]


class AccountResponse(BaseModel):
    """Cuenta confirmada por el comando."""

    user_id: UUID
    full_name: str
    email: str
    actor_type: ActorType
    status: AccountStatus
    version: int
    updated_at: Annotated[datetime, Field(description=INSTANT_DESCRIPTION)]


class AccountReadResponse(BaseModel):
    """Cuenta proyectada en `user_directory` (consistencia eventual)."""

    user_id: UUID
    full_name: str
    email: str
    actor_type: ActorType
    status: AccountStatus
    version: Annotated[int, Field(description="Versión de la cuenta ya proyectada.")]
    projected_at: Annotated[datetime, Field(description="Última proyección aplicada, en UTC.")]


def parse_account_change(body: object) -> AccountChangeInput:
    """Forma del JSON: campos permitidos, tipos y formato del correo."""
    if not isinstance(body, dict):
        return AccountChangeInput(
            transport_violations=(
                Violation(None, Issue.REQUIRED if body is None else Issue.UNKNOWN_VALUE),
            )
        )
    violations = [
        Violation(name, Issue.FIELD_NOT_ALLOWED) for name in body if name not in ACCOUNT_FIELDS
    ]
    expected_version = body.get("expected_version")
    if expected_version is not None and (
        not isinstance(expected_version, int) or isinstance(expected_version, bool)
    ):
        violations.append(Violation("expected_version", Issue.UNKNOWN_VALUE))
        expected_version = None
    full_name = body.get("full_name")
    if full_name is not None and not isinstance(full_name, str):
        violations.append(Violation("full_name", Issue.UNKNOWN_VALUE))
        full_name = None
    email = body.get("email")
    if email is not None:
        email = _email(email, violations)
    return AccountChangeInput(
        expected_version=expected_version,
        full_name=full_name,
        email=email,
        transport_violations=tuple(violations),
    )


def _email(value: object, violations: list[Violation]) -> str | None:
    if not isinstance(value, str):
        violations.append(Violation("email", Issue.UNKNOWN_VALUE))
        return None
    trimmed = value.strip()
    if not 1 <= len(trimmed) <= EMAIL_MAX_LENGTH:
        return trimmed  # La longitud la informa el dominio (`invalid_length`).
    try:
        return str(_EMAIL.validate_python(trimmed))
    except ValidationError:
        violations.append(Violation("email", Issue.UNKNOWN_VALUE))
        return None


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = NO_STORE
    return response


def _put_account(
    verify_token: TokenVerifier,
    update_account: UpdateAccount,
    *,
    raw_body: bytes,
    raw_key: str | None,
    authorization: str | None,
) -> Response:
    """Autenticar → validar cuerpo y clave → idempotencia → versión → unicidad."""
    user_id = authenticate_user(verify_token, authorization)
    if isinstance(user_id, Response):
        return _no_store(user_id)
    change = parse_account_change(decode_body(raw_body))
    key, key_problem = parse_idempotency_key(raw_key)
    try:
        if key is None:
            # La clave es inválida: se informan también las reglas del cuerpo.
            problems = [key_problem] if key_problem else []
            try:
                update_account.validate(change)
            except InvalidProfileSection as error:
                problems.extend(error.violations)
            return _no_store(validation_failure(problems))
        result = update_account(
            user_id,
            UpdateAccountCommand(
                change=change, idempotency_key=key, correlation_id=correlation_uuid()
            ),
        )
    except (
        InvalidProfileSection,
        AccountVersionConflict,
        EmailAlreadyRegistered,
        IdempotencyKeyReused,
        AccountNotFound,
    ) as error:
        return _no_store(_command_failure(error))
    response = JSONResponse(status_code=HTTPStatus.OK, content=result.document)
    if result.replayed:
        response.headers[REPLAYED_HEADER] = "true"
    return _no_store(response)


def _command_failure(error: Exception) -> Response:
    if isinstance(error, InvalidProfileSection):
        return validation_failure(error.violations)
    if isinstance(error, AccountVersionConflict):
        return error_response(
            HTTPStatus.CONFLICT,
            error.code,
            "La cuenta cambió desde la última lectura; recargue y vuelva a intentar.",
            [ErrorDetail(field="expected_version", issue=str(error.current_version))],
        )
    if isinstance(error, EmailAlreadyRegistered):
        # Mismo código y mensaje que el registro (HU001): no revela nada más de la otra cuenta.
        return error_response(HTTPStatus.CONFLICT, error.code, "El correo ya está registrado.")
    if isinstance(error, IdempotencyKeyReused):
        return error_response(
            HTTPStatus.CONFLICT,
            error.code,
            "La clave de idempotencia ya se usó con otra solicitud.",
        )
    # `AccountNotFound`: el `sub` del token no corresponde a ninguna cuenta.
    return unauthenticated()


_UNAUTHORIZED: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNAUTHORIZED.value: {
        "model": ErrorResponse,
        "description": "`invalid_access_token`: falta el token o no es válido.",
    }
}


def build_account_router(
    *, verify_token: TokenVerifier, update_account: UpdateAccount, get_account: GetAccount
) -> APIRouter:
    router = APIRouter(prefix="/v1/me", tags=["account"])

    @router.put(
        "/account",
        response_model=AccountResponse,
        summary="Editar nombre y correo de la cuenta propia (HU002)",
        description="Cualquier actor. El cambio de correo no revoca sesiones ni exige "
        "verificación (ADR-035 §4): el siguiente inicio de sesión usa el correo nuevo. La "
        "respuesta no espera la proyección.",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": AccountUpdateRequest.model_json_schema()}
                },
            }
        },
        responses={
            **_UNAUTHORIZED,
            HTTPStatus.OK.value: {
                "description": "Cuenta confirmada (o reintento con la misma clave).",
                "headers": {
                    REPLAYED_HEADER: {
                        "description": "`true` cuando se devuelve el resultado de un reintento.",
                        "schema": {"type": "string"},
                    }
                },
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`version_conflict` (`details[0].issue` = versión vigente), "
                "`email_already_registered` (sin más información) o `idempotency_key_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` con `details[{field, issue}]` (ADR-034); "
                "`user_id`, `actor_type`, `status` o `password` → `field_not_allowed`.",
            },
        },
    )
    async def put_account(
        request: Request,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        # Solo se leen los bytes aquí; el trabajo bloqueante (token y base) corre en el pool.
        return await run_in_threadpool(
            _put_account,
            verify_token,
            update_account,
            raw_body=await request.body(),
            raw_key=idempotency_key,
            authorization=authorization,
        )

    @router.get(
        "/account",
        response_model=AccountReadResponse,
        summary="Consultar la cuenta propia desde la proyección (HU002)",
        responses={
            **_UNAUTHORIZED,
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": f"`{USER_NOT_FOUND}`: la cuenta aún no está proyectada.",
            },
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": f"`{ReadModelUnavailable.code}`: la base de lectura no responde.",
            },
        },
    )
    def read_account(
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | AccountReadResponse:
        user_id = authenticate_user(verify_token, authorization)
        if isinstance(user_id, Response):
            return _no_store(user_id)
        try:
            view = get_account(user_id)
        except ReadModelUnavailable as error:
            return _no_store(
                error_response(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    error.code,
                    "No fue posible consultar la cuenta. Intente de nuevo.",
                )
            )
        if view is None:
            return _no_store(
                error_response(
                    HTTPStatus.NOT_FOUND,
                    USER_NOT_FOUND,
                    "La cuenta todavía no está disponible. Intente de nuevo.",
                )
            )
        response.headers["Cache-Control"] = NO_STORE
        return AccountReadResponse(
            user_id=view.user_id,
            full_name=view.full_name,
            email=view.email,
            actor_type=ActorType(view.actor_type),
            status=AccountStatus(view.status),
            version=view.version,
            projected_at=view.projected_at.astimezone(UTC),
        )

    return router
