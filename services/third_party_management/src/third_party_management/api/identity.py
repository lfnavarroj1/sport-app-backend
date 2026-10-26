"""Identidad de quien llama y errores comunes de HU031.

El BFF móvil reenvía el access token del usuario; el servicio lo vuelve a verificar
(firma, emisor, audiencia y vigencia) con el JWKS de `users_management` y deriva de él
`user_id` y `actor_type`. No acepta identidades enviadas en el cuerpo.
"""

import logging
from collections.abc import Callable
from http import HTTPStatus
from typing import Any
from uuid import UUID

from fastapi import Response

from sportapp_platform.correlation import current_correlation_id
from sportapp_platform.errors import ErrorResponse, error_response
from sportapp_platform.tokens import InvalidAccessToken, JwksUnavailable, VerifiedAccessToken
from third_party_management.application.errors import (
    CapabilitiesUnavailable,
    CapabilityRequired,
    PermissionRequired,
)
from third_party_management.application.ports import Caller

BEARER_PREFIX = "bearer "
UNAVAILABLE_MESSAGE = "No fue posible verificar la autorización. Intente de nuevo."

_logger = logging.getLogger("sportapp.integrations")

TokenVerifier = Callable[[str], VerifiedAccessToken]

AUTH_RESPONSES: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNAUTHORIZED.value: {
        "model": ErrorResponse,
        "description": "`invalid_access_token`: falta el token o no es válido.",
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


def unavailable() -> Response:
    return error_response(
        HTTPStatus.SERVICE_UNAVAILABLE, CapabilitiesUnavailable.code, UNAVAILABLE_MESSAGE
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
        _logger.warning("integrations.jwks_unavailable")
        return unavailable()
    if identity.actor_type is None:
        # Sin actor no hay permisos que evaluar: se trata como operación no permitida.
        return forbidden(PermissionRequired())
    return Caller(user_id=user_id, actor_type=identity.actor_type)


_FORBIDDEN_MESSAGES = {
    PermissionRequired.code: "Su tipo de usuario no puede realizar esta operación.",
    CapabilityRequired.code: "Su plan no incluye esta funcionalidad.",
}


def forbidden(error: PermissionRequired | CapabilityRequired) -> Response:
    return error_response(HTTPStatus.FORBIDDEN, error.code, _FORBIDDEN_MESSAGES[error.code])
