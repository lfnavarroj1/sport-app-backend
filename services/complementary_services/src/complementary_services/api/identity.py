"""Identidad de quien llama: el BFF reenvía el access token y el servicio lo vuelve a
verificar (firma, emisor, audiencia y vigencia) con el JWKS de `users_management`."""

import logging
from collections.abc import Callable
from http import HTTPStatus
from typing import Any
from uuid import UUID

from fastapi import Response

from complementary_services.application.errors import PermissionRequired
from complementary_services.application.ports import Caller
from sportapp_platform.correlation import current_correlation_id
from sportapp_platform.errors import ErrorResponse, error_response
from sportapp_platform.tokens import InvalidAccessToken, JwksUnavailable, VerifiedAccessToken

BEARER_PREFIX = "bearer "
UPSTREAM_UNAVAILABLE = "upstream_unavailable"

_logger = logging.getLogger("sportapp.services")

TokenVerifier = Callable[[str], VerifiedAccessToken]

AUTH_RESPONSES: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNAUTHORIZED.value: {
        "model": ErrorResponse,
        "description": "`invalid_access_token`: falta el token o no es válido.",
    },
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required`: el actor no tiene el permiso de la operación.",
    },
    HTTPStatus.SERVICE_UNAVAILABLE.value: {
        "model": ErrorResponse,
        "description": f"`{UPSTREAM_UNAVAILABLE}`: no se pudo verificar la identidad.",
    },
}


def correlation_uuid() -> UUID | None:
    correlation_id = current_correlation_id()
    return UUID(correlation_id) if correlation_id else None


def unauthenticated() -> Response:
    response = error_response(
        HTTPStatus.UNAUTHORIZED, InvalidAccessToken.code, "La sesión no es válida."
    )
    response.headers["WWW-Authenticate"] = "Bearer"
    return response


def forbidden(error: PermissionRequired) -> Response:
    return error_response(
        HTTPStatus.FORBIDDEN, error.code, "Su tipo de usuario no puede realizar esta operación."
    )


def authenticate(verify_token: TokenVerifier, authorization: str | None) -> Caller | Response:
    """Devuelve el `Caller` verificado o la respuesta de error que corresponde."""
    if not authorization or not authorization.lower().startswith(BEARER_PREFIX):
        return unauthenticated()
    token = authorization[len(BEARER_PREFIX) :].strip()
    if not token:
        return unauthenticated()
    try:
        identity = verify_token(token)
        user_id = UUID(identity.subject)
    except (InvalidAccessToken, ValueError):
        return unauthenticated()
    except JwksUnavailable:
        _logger.warning("services.jwks_unavailable")
        return error_response(
            HTTPStatus.SERVICE_UNAVAILABLE,
            UPSTREAM_UNAVAILABLE,
            "No fue posible verificar la sesión. Intente de nuevo.",
        )
    if identity.actor_type is None:
        return forbidden(PermissionRequired())
    return Caller(user_id=user_id, actor_type=identity.actor_type)
