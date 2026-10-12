"""HU003 en el canal web: `POST /v1/sessions` y `POST /v1/sessions/refresh` (contrato 0.4.0).

El BFF valida el transporte, agrega la IP del usuario y reenvía al servicio
propietario. Verificar credenciales, limitar intentos y emitir tokens son reglas
de `users_management`: el BFF no genera JWT ni implementa hash.

Adaptación del canal web (aprobada el 2026-10-11): el refresh token no viaja en
el cuerpo, sino en una cookie `HttpOnly` que el JavaScript del navegador no puede
leer. Como `/refresh` se autentica solo con esa cookie, ambas operaciones exigen
un `Origin` de la lista permitida (protección CSRF).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Cookie, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints

from bff_web.clients.users_management import (
    UpstreamTimeout,
    UpstreamUnavailable,
    UsersManagementClient,
)
from sportapp_platform.errors import ErrorResponse, error_response

RETRY_AFTER_HEADER = "Retry-After"
# `__Secure-`: el navegador solo la acepta con `Secure` y desde un origen seguro.
# (`__Host-` exigiría `Path=/`; se prefiere limitarla a las rutas de sesión.)
REFRESH_COOKIE = "__Secure-sportapp_refresh"
REFRESH_COOKIE_PATH = "/v1/sessions"
REFRESH_TOKEN_MIN_LENGTH = 20
REFRESH_TOKEN_MAX_LENGTH = 256
ORIGIN_NOT_ALLOWED = "origin_not_allowed"
INVALID_SESSION_CODE = "invalid_refresh_token"
SameSite = Literal["strict", "lax", "none"]

_PASSTHROUGH_STATUSES = {
    HTTPStatus.OK,
    HTTPStatus.UNAUTHORIZED,
    HTTPStatus.UNPROCESSABLE_ENTITY,
    HTTPStatus.TOO_MANY_REQUESTS,
}
_PASSTHROUGH_HEADERS = ("Cache-Control", RETRY_AFTER_HEADER)
_UNAVAILABLE_MESSAGE = "El servicio de usuarios no está disponible."

_logger = logging.getLogger("sportapp.bff")


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=254)]
    password: Annotated[SecretStr, Field(min_length=1, max_length=128)]


class SessionUser(BaseModel):
    user_id: UUID
    full_name: str
    email: str
    actor_type: Literal["athlete", "organizer", "service_provider"]


class WebSessionResponse(BaseModel):
    """Sin `refresh_token`: viaja en la cookie `HttpOnly`."""

    # Tipo de token según RFC 6750, no una credencial.
    token_type: Literal["Bearer"]
    access_token: str
    expires_in: int = Field(description="Segundos de vigencia del access token.")
    refresh_expires_in: int = Field(
        description="Segundos de vigencia de la sesión (refresh token en la cookie)."
    )
    user: SessionUser


@dataclass(frozen=True)
class RefreshCookie:
    """Atributos de la cookie; `same_site` depende de si web y BFF comparten sitio."""

    same_site: SameSite

    def attach(self, response: Response, token: str, max_age: int) -> None:
        response.set_cookie(
            REFRESH_COOKIE,
            token,
            max_age=max_age,
            path=REFRESH_COOKIE_PATH,
            secure=True,
            httponly=True,
            samesite=self.same_site,
        )

    def clear(self, response: Response) -> None:
        response.delete_cookie(
            REFRESH_COOKIE,
            path=REFRESH_COOKIE_PATH,
            secure=True,
            httponly=True,
            samesite=self.same_site,
        )


_SET_COOKIE_HEADER = {
    "Set-Cookie": {
        "description": f"`{REFRESH_COOKIE}=<refresh token>; HttpOnly; Secure; "
        f"Path={REFRESH_COOKIE_PATH}; SameSite=<configurado>; Max-Age=<refresh_expires_in>`.",
        "schema": {"type": "string"},
    }
}
_ORIGIN_ERROR: dict[str, Any] = {
    "model": ErrorResponse,
    "description": f"`{ORIGIN_NOT_ALLOWED}`: falta `Origin` o no está en la lista permitida.",
}
_UPSTREAM_ERRORS: dict[int | str, dict[str, Any]] = {
    HTTPStatus.FORBIDDEN.value: _ORIGIN_ERROR,
    HTTPStatus.SERVICE_UNAVAILABLE.value: {
        "model": ErrorResponse,
        "description": "`upstream_unavailable`.",
    },
    HTTPStatus.GATEWAY_TIMEOUT.value: {
        "model": ErrorResponse,
        "description": "`upstream_timeout`.",
    },
}


def _call(send: Callable[[], httpx.Response]) -> httpx.Response | Response:
    """Respuesta del servicio, o el error del BFF si no respondió como se esperaba."""
    try:
        upstream = send()
    except UpstreamTimeout as error:
        return error_response(
            HTTPStatus.GATEWAY_TIMEOUT, error.code, "El servicio de usuarios no respondió."
        )
    except UpstreamUnavailable as error:
        return error_response(HTTPStatus.SERVICE_UNAVAILABLE, error.code, _UNAVAILABLE_MESSAGE)
    if upstream.status_code not in _PASSTHROUGH_STATUSES:
        _logger.warning(
            "bff.upstream_unexpected_status",
            extra={"fields": {"upstream": "users-management", "status": upstream.status_code}},
        )
        return error_response(
            HTTPStatus.SERVICE_UNAVAILABLE, UpstreamUnavailable.code, _UNAVAILABLE_MESSAGE
        )
    return upstream


def _passthrough(upstream: httpx.Response) -> Response:
    headers = {
        name: upstream.headers[name] for name in _PASSTHROUGH_HEADERS if name in upstream.headers
    }
    return JSONResponse(status_code=upstream.status_code, content=upstream.json(), headers=headers)


def build_sessions_router(
    users: UsersManagementClient,
    *,
    allowed_origins: tuple[str, ...],
    cookie: RefreshCookie,
) -> APIRouter:
    router = APIRouter(prefix="/v1/sessions", tags=["sessions"])

    def rejected_origin(request: Request) -> Response | None:
        if request.headers.get("origin") in allowed_origins:
            return None
        _logger.warning("bff.origin_rejected", extra={"fields": {"route": request.url.path}})
        return error_response(HTTPStatus.FORBIDDEN, ORIGIN_NOT_ALLOWED, "Origen no permitido.")

    def invalid_refresh() -> Response:
        response = error_response(
            HTTPStatus.UNAUTHORIZED,
            INVALID_SESSION_CODE,
            "La sesión no es válida. Inicie sesión de nuevo.",
        )
        cookie.clear(response)
        return response

    def with_cookie(upstream: httpx.Response) -> Response:
        body = upstream.json()
        token = body.pop("refresh_token", None)
        if not isinstance(token, str):
            _logger.warning(
                "bff.upstream_contract_mismatch",
                extra={"fields": {"upstream": "users-management", "missing": "refresh_token"}},
            )
            return error_response(
                HTTPStatus.SERVICE_UNAVAILABLE, UpstreamUnavailable.code, _UNAVAILABLE_MESSAGE
            )
        response = JSONResponse(content=body, headers={"Cache-Control": "no-store"})
        cookie.attach(response, token, int(body["refresh_expires_in"]))
        return response

    @router.post(
        "",
        response_model=WebSessionResponse,
        summary="Iniciar sesión (HU003)",
        responses={
            HTTPStatus.OK.value: {"headers": _SET_COOKIE_HEADER},
            HTTPStatus.UNAUTHORIZED.value: {
                "model": ErrorResponse,
                "description": "`invalid_credentials`: credenciales incorrectas o cuenta "
                "deshabilitada (mensaje genérico).",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`.",
            },
            HTTPStatus.TOO_MANY_REQUESTS.value: {
                "model": ErrorResponse,
                "description": "`too_many_attempts`; `Retry-After` indica los segundos de espera.",
                "headers": {RETRY_AFTER_HEADER: {"schema": {"type": "integer"}}},
            },
            **_UPSTREAM_ERRORS,
        },
    )
    def create_session(body: LoginRequest, request: Request) -> Response:
        if (rejected := rejected_origin(request)) is not None:
            return rejected
        payload = {"email": body.email, "password": body.password.get_secret_value()}
        client_ip = request.client.host if request.client else None
        upstream = _call(lambda: users.create_session(payload, client_ip))
        if isinstance(upstream, Response):
            return upstream
        if upstream.status_code == HTTPStatus.OK:
            return with_cookie(upstream)
        return _passthrough(upstream)

    @router.post(
        "/refresh",
        response_model=WebSessionResponse,
        summary="Renovar la sesión con la cookie (HU003)",
        description=f"Sin cuerpo: usa la cookie `{REFRESH_COOKIE}`, la rota y devuelve un "
        "access token nuevo. Si la sesión no es válida, borra la cookie.",
        responses={
            HTTPStatus.OK.value: {"headers": _SET_COOKIE_HEADER},
            HTTPStatus.UNAUTHORIZED.value: {
                "model": ErrorResponse,
                "description": f"`{INVALID_SESSION_CODE}`: sin cookie, o vencida, revocada o "
                "reutilizada. La respuesta borra la cookie.",
            },
            **_UPSTREAM_ERRORS,
        },
    )
    def refresh(
        request: Request,
        refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
    ) -> Response:
        if (rejected := rejected_origin(request)) is not None:
            return rejected
        if (
            refresh_token is None
            or not REFRESH_TOKEN_MIN_LENGTH <= len(refresh_token) <= REFRESH_TOKEN_MAX_LENGTH
        ):
            return invalid_refresh()
        token = refresh_token
        upstream = _call(lambda: users.refresh_session({"refresh_token": token}))
        if isinstance(upstream, Response):
            return upstream
        if upstream.status_code == HTTPStatus.OK:
            return with_cookie(upstream)
        # 401 o 422 del servicio: la cookie no sirve y el usuario no puede corregirla.
        return invalid_refresh()

    @router.post(
        "/revoke",
        status_code=HTTPStatus.NO_CONTENT,
        summary="Cerrar sesión con la cookie (HU004)",
        description=f"Sin cuerpo: revoca la sesión de la cookie `{REFRESH_COOKIE}` y la borra. "
        "Siempre `204` si el servicio confirma, también sin cookie o con una sesión ya cerrada. "
        "Si el servicio no responde (`503`/`504`) la cookie también se borra: el cierre local "
        "queda garantizado, pero la revocación remota no se confirmó.",
        responses={
            HTTPStatus.NO_CONTENT.value: {
                "description": "Sesión cerrada; la respuesta borra la cookie.",
            },
            **_UPSTREAM_ERRORS,
        },
    )
    def revoke(
        request: Request,
        refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
    ) -> Response:
        # Un origen ajeno no puede cerrar la sesión del usuario (CSRF): no se toca la cookie.
        if (rejected := rejected_origin(request)) is not None:
            return rejected
        closed = Response(status_code=HTTPStatus.NO_CONTENT, headers={"Cache-Control": "no-store"})
        if (
            refresh_token is None
            or not REFRESH_TOKEN_MIN_LENGTH <= len(refresh_token) <= REFRESH_TOKEN_MAX_LENGTH
        ):
            cookie.clear(closed)
            return closed
        token = refresh_token
        try:
            upstream = users.revoke_session({"refresh_token": token})
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            failed = error_response(
                HTTPStatus.GATEWAY_TIMEOUT
                if isinstance(error, UpstreamTimeout)
                else HTTPStatus.SERVICE_UNAVAILABLE,
                error.code,
                "Se cerró la sesión en este navegador, pero no se confirmó en el servidor.",
            )
            cookie.clear(failed)
            return failed
        if upstream.status_code not in (HTTPStatus.NO_CONTENT, HTTPStatus.UNPROCESSABLE_ENTITY):
            _logger.warning(
                "bff.upstream_unexpected_status",
                extra={"fields": {"upstream": "users-management", "status": upstream.status_code}},
            )
            failed = error_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                UpstreamUnavailable.code,
                "Se cerró la sesión en este navegador, pero no se confirmó en el servidor.",
            )
            cookie.clear(failed)
            return failed
        # `422`: la cookie no tiene la forma de un refresh token; no hay sesión que revocar.
        cookie.clear(closed)
        return closed

    return router
