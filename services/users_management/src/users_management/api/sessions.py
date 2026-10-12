"""HU003: `POST /v1/sessions`, `POST /v1/sessions/refresh` y `GET /.well-known/jwks.json`.
HU004: `POST /v1/sessions/revoke` (contrato aprobado el 2026-10-12).

Contrato aprobado el 2026-10-10: tokens en el cuerpo JSON, access JWT EdDSA de
corta vida y refresh token opaco rotativo. Los errores de credenciales son
genéricos para no revelar si un correo existe.
"""

import ipaddress
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints

from sportapp_platform.correlation import current_correlation_id
from sportapp_platform.errors import ErrorResponse, error_response
from users_management.application.commands.login import Login, LoginCommand
from users_management.application.commands.refresh_session import (
    RefreshSession,
    RefreshSessionCommand,
)
from users_management.application.commands.revoke_session import (
    RevokeSession,
    RevokeSessionCommand,
)
from users_management.application.commands.sessions import SessionResult
from users_management.domain.account.model import EMAIL_MAX_LENGTH, PASSWORD_MAX_LENGTH, ActorType
from users_management.domain.authentication.errors import (
    InvalidCredentials,
    InvalidRefreshToken,
    TooManyLoginAttempts,
)

CLIENT_IP_HEADER = "X-Client-IP"
IDEMPOTENCY_HEADER = "Idempotency-Key"
RETRY_AFTER_HEADER = "Retry-After"
# Los tokens son credenciales: ninguna caché intermedia debe guardarlos.
NO_STORE = {"Cache-Control": "no-store"}
UNKNOWN_CLIENT = "unknown"


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=EMAIL_MAX_LENGTH)
    ]
    # Sin mínimo: la política de longitud aplica al registro, no a quien intenta entrar.
    password: Annotated[SecretStr, Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)]


class RefreshSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: Annotated[SecretStr, Field(min_length=20, max_length=256)]


class SessionUser(BaseModel):
    user_id: UUID
    full_name: str
    email: str
    actor_type: ActorType


class SessionResponse(BaseModel):
    # Tipo de token según RFC 6750, no una credencial.
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105  # nosec B105
    access_token: str
    expires_in: int = Field(description="Segundos de vigencia del access token.")
    refresh_token: str
    refresh_expires_in: int = Field(description="Segundos de vigencia del refresh token.")
    user: SessionUser

    @classmethod
    def from_result(cls, result: SessionResult) -> "SessionResponse":
        return cls(
            access_token=result.access_token,
            expires_in=result.access_token_expires_in,
            refresh_token=result.refresh_token,
            refresh_expires_in=result.refresh_token_expires_in,
            user=SessionUser(
                user_id=result.user_id,
                full_name=result.full_name,
                email=result.email,
                actor_type=result.actor_type,
            ),
        )


class JsonWebKey(BaseModel):
    kty: str
    crv: str
    x: str
    kid: str
    alg: str
    use: str


class JsonWebKeySet(BaseModel):
    keys: list[JsonWebKey]


def _client_ip(request: Request, forwarded: str | None) -> str:
    """IP del usuario según el BFF; si falta o no es una IP, la del par de la conexión."""
    if forwarded:
        try:
            return str(ipaddress.ip_address(forwarded.strip()))
        except ValueError:
            pass
    return request.client.host if request.client else UNKNOWN_CLIENT


def _correlation_uuid() -> UUID | None:
    correlation_id = current_correlation_id()
    return UUID(correlation_id) if correlation_id else None


_INVALID_CREDENTIALS = {
    "model": ErrorResponse,
    "description": "`invalid_credentials`: credenciales incorrectas o cuenta deshabilitada.",
}
_VALIDATION = {"model": ErrorResponse, "description": "`validation_error`."}


def build_sessions_router(
    login: Login, refresh_session: RefreshSession, revoke_session: RevokeSession
) -> APIRouter:
    router = APIRouter(prefix="/v1/sessions", tags=["sessions"])

    @router.post(
        "",
        response_model=SessionResponse,
        summary="Iniciar sesión (HU003)",
        responses={
            HTTPStatus.UNAUTHORIZED.value: _INVALID_CREDENTIALS,
            HTTPStatus.UNPROCESSABLE_ENTITY.value: _VALIDATION,
            HTTPStatus.TOO_MANY_REQUESTS.value: {
                "model": ErrorResponse,
                "description": "`too_many_attempts`: demasiados fallos para este correo e IP.",
                "headers": {
                    RETRY_AFTER_HEADER: {
                        "description": "Segundos que faltan para volver a intentar.",
                        "schema": {"type": "integer"},
                    }
                },
            },
        },
    )
    def create_session(
        body: LoginRequest,
        request: Request,
        response: Response,
        client_ip: Annotated[str | None, Header(alias=CLIENT_IP_HEADER)] = None,
    ) -> Response | SessionResponse:
        command = LoginCommand(
            email=body.email,
            password=body.password.get_secret_value(),
            client_ip=_client_ip(request, client_ip),
            correlation_id=_correlation_uuid(),
        )
        try:
            result = login(command)
        except TooManyLoginAttempts as error:
            limited = error_response(
                HTTPStatus.TOO_MANY_REQUESTS,
                error.code,
                "Demasiados intentos fallidos. Intente de nuevo más tarde.",
            )
            limited.headers[RETRY_AFTER_HEADER] = str(error.retry_after_seconds)
            return limited
        except InvalidCredentials as error:
            return error_response(
                HTTPStatus.UNAUTHORIZED, error.code, "Correo o contraseña incorrectos."
            )
        response.headers.update(NO_STORE)
        return SessionResponse.from_result(result)

    @router.post(
        "/refresh",
        response_model=SessionResponse,
        summary="Renovar la sesión (HU003)",
        responses={
            HTTPStatus.UNAUTHORIZED.value: {
                "model": ErrorResponse,
                "description": "`invalid_refresh_token`: vencido, revocado, reutilizado o "
                "de una cuenta deshabilitada.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: _VALIDATION,
        },
    )
    def refresh(body: RefreshSessionRequest, response: Response) -> Response | SessionResponse:
        command = RefreshSessionCommand(
            refresh_token=body.refresh_token.get_secret_value(),
            correlation_id=_correlation_uuid(),
        )
        try:
            result = refresh_session(command)
        except InvalidRefreshToken as error:
            return error_response(
                HTTPStatus.UNAUTHORIZED,
                error.code,
                "La sesión no es válida. Inicie sesión de nuevo.",
            )
        response.headers.update(NO_STORE)
        return SessionResponse.from_result(result)

    @router.post(
        "/revoke",
        status_code=HTTPStatus.NO_CONTENT,
        summary="Cerrar sesión: revocar la credencial renovable (HU004)",
        description="Revoca toda la sesión (familia) del refresh token. Siempre `204`, también "
        "si el token es desconocido, vencido o ya revocado: es idempotente y no revela nada. "
        "El access token vigente expira por su vigencia corta. `Idempotency-Key` (UUID) es "
        "opcional: la cola offline móvil la repite en cada reintento (ADR-026).",
        responses={HTTPStatus.UNPROCESSABLE_ENTITY.value: _VALIDATION},
    )
    def revoke(
        body: RefreshSessionRequest,
        idempotency_key: Annotated[UUID | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
    ) -> Response:
        revoke_session(
            RevokeSessionCommand(
                refresh_token=body.refresh_token.get_secret_value(),
                correlation_id=_correlation_uuid(),
                idempotency_key=idempotency_key,
            )
        )
        return Response(status_code=HTTPStatus.NO_CONTENT, headers=NO_STORE)

    return router


def build_jwks_router(jwks: dict[str, Any]) -> APIRouter:
    """Clave pública para que BFF y servicios validen los access tokens (CA5)."""
    router = APIRouter(tags=["sessions"])

    @router.get(
        "/.well-known/jwks.json",
        response_model=JsonWebKeySet,
        summary="Claves públicas de firma de access tokens (HU003)",
    )
    def get_jwks(response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "public, max-age=300"
        return jwks

    return router
