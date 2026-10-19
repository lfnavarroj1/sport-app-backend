"""Identidad de quien llama al perfil (HU005): el BFF reenvía el access token y el
servicio lo vuelve a verificar (firma, emisor, audiencia y vigencia) con su propio JWKS.

Solo el actor `athlete` registra y consulta su perfil (ADR-034 §1); la identidad sale
siempre del token, nunca del cuerpo.
"""

from collections.abc import Callable
from http import HTTPStatus
from typing import Any
from uuid import UUID

from fastapi import Response

from sportapp_platform.correlation import current_correlation_id
from sportapp_platform.errors import ErrorResponse, error_response
from sportapp_platform.tokens import InvalidAccessToken, VerifiedAccessToken
from users_management.domain.account.model import ActorType

BEARER_PREFIX = "bearer "
PERMISSION_REQUIRED = "permission_required"

TokenVerifier = Callable[[str], VerifiedAccessToken]

AUTH_RESPONSES: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNAUTHORIZED.value: {
        "model": ErrorResponse,
        "description": "`invalid_access_token`: falta el token o no es válido.",
    },
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": f"`{PERMISSION_REQUIRED}`: solo el deportista (`athlete`) puede operar.",
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


def forbidden() -> Response:
    return error_response(
        HTTPStatus.FORBIDDEN,
        PERMISSION_REQUIRED,
        "Su tipo de usuario no puede realizar esta operación.",
    )


def authenticate_athlete(verify_token: TokenVerifier, authorization: str | None) -> UUID | Response:
    """Devuelve el `user_id` del deportista verificado o la respuesta de error."""
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
    if identity.actor_type != ActorType.ATHLETE.value:
        return forbidden()
    return user_id
