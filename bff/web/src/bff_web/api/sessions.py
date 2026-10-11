"""HU003 en el canal web: `POST /v1/sessions` y `POST /v1/sessions/refresh`.

El BFF valida el transporte, agrega la IP del usuario y reenvía al servicio
propietario. Verificar credenciales, limitar intentos y emitir tokens son reglas
de `users_management`: el BFF no genera JWT ni implementa hash, y entrega sus
respuestas sin alterar (incluido `Retry-After`).
"""

import logging
from collections.abc import Callable
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints

from bff_web.clients.users_management import (
    UpstreamTimeout,
    UpstreamUnavailable,
    UsersManagementClient,
)
from sportapp_platform.errors import ErrorResponse, error_response

RETRY_AFTER_HEADER = "Retry-After"
_PASSTHROUGH_STATUSES = {
    HTTPStatus.OK,
    HTTPStatus.UNAUTHORIZED,
    HTTPStatus.UNPROCESSABLE_ENTITY,
    HTTPStatus.TOO_MANY_REQUESTS,
}
_PASSTHROUGH_HEADERS = ("Cache-Control", RETRY_AFTER_HEADER)

_logger = logging.getLogger("sportapp.bff")


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=254)]
    password: Annotated[SecretStr, Field(min_length=1, max_length=128)]


class RefreshSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: Annotated[SecretStr, Field(min_length=20, max_length=256)]


class SessionUser(BaseModel):
    user_id: UUID
    full_name: str
    email: str
    actor_type: Literal["athlete", "organizer", "service_provider"]


class SessionResponse(BaseModel):
    # Tipo de token según RFC 6750, no una credencial.
    token_type: Literal["Bearer"]  # nosec B105
    access_token: str
    expires_in: int = Field(description="Segundos de vigencia del access token.")
    refresh_token: str
    refresh_expires_in: int = Field(description="Segundos de vigencia del refresh token.")
    user: SessionUser


_UPSTREAM_ERRORS: dict[int | str, dict[str, Any]] = {
    HTTPStatus.SERVICE_UNAVAILABLE.value: {
        "model": ErrorResponse,
        "description": "`upstream_unavailable`.",
    },
    HTTPStatus.GATEWAY_TIMEOUT.value: {
        "model": ErrorResponse,
        "description": "`upstream_timeout`.",
    },
}
_VALIDATION: dict[str, Any] = {"model": ErrorResponse, "description": "`validation_error`."}


def _relay(send: Callable[[], httpx.Response]) -> Response:
    try:
        upstream = send()
    except UpstreamTimeout as error:
        return error_response(
            HTTPStatus.GATEWAY_TIMEOUT, error.code, "El servicio de usuarios no respondió."
        )
    except UpstreamUnavailable as error:
        return error_response(
            HTTPStatus.SERVICE_UNAVAILABLE,
            error.code,
            "El servicio de usuarios no está disponible.",
        )
    if upstream.status_code not in _PASSTHROUGH_STATUSES:
        _logger.warning(
            "bff.upstream_unexpected_status",
            extra={"fields": {"upstream": "users-management", "status": upstream.status_code}},
        )
        return error_response(
            HTTPStatus.SERVICE_UNAVAILABLE,
            UpstreamUnavailable.code,
            "El servicio de usuarios no está disponible.",
        )
    headers = {
        name: upstream.headers[name] for name in _PASSTHROUGH_HEADERS if name in upstream.headers
    }
    return JSONResponse(status_code=upstream.status_code, content=upstream.json(), headers=headers)


def build_sessions_router(users: UsersManagementClient) -> APIRouter:
    router = APIRouter(prefix="/v1/sessions", tags=["sessions"])

    @router.post(
        "",
        response_model=SessionResponse,
        summary="Iniciar sesión (HU003)",
        responses={
            HTTPStatus.UNAUTHORIZED.value: {
                "model": ErrorResponse,
                "description": "`invalid_credentials`: credenciales incorrectas o cuenta "
                "deshabilitada (mensaje genérico).",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: _VALIDATION,
            HTTPStatus.TOO_MANY_REQUESTS.value: {
                "model": ErrorResponse,
                "description": "`too_many_attempts`; `Retry-After` indica los segundos de espera.",
                "headers": {RETRY_AFTER_HEADER: {"schema": {"type": "integer"}}},
            },
            **_UPSTREAM_ERRORS,
        },
    )
    def create_session(body: LoginRequest, request: Request) -> Response:
        payload = {"email": body.email, "password": body.password.get_secret_value()}
        client_ip = request.client.host if request.client else None
        return _relay(lambda: users.create_session(payload, client_ip))

    @router.post(
        "/refresh",
        response_model=SessionResponse,
        summary="Renovar la sesión (HU003)",
        responses={
            HTTPStatus.UNAUTHORIZED.value: {
                "model": ErrorResponse,
                "description": "`invalid_refresh_token`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: _VALIDATION,
            **_UPSTREAM_ERRORS,
        },
    )
    def refresh(body: RefreshSessionRequest) -> Response:
        payload = {"refresh_token": body.refresh_token.get_secret_value()}
        return _relay(lambda: users.refresh_session(payload))

    return router
