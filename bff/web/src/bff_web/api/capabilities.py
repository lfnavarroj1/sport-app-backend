"""HU003 CA7-CA10: `GET /v1/me/capabilities` (ADR-024 y ADR-025).

El BFF compone, sin persistir nada:
- identidad y `actor_type`: access token firmado por `users_management` (JWKS);
- permisos por actor: contrato `actor-permissions.v1`, filtrado por el canal;
- capacidades del deportista: instantánea vigente de `affiliation_management`.

El usuario sale del token: el cliente no puede consultar a otro. Un actor
desconocido no recibe un rol por defecto (`403 unsupported_actor_type`) y, si
Afiliación no responde, nunca se concede `FREE` por suposición (`503`). Rutas,
etiquetas e iconos pertenecen al cliente.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Response
from pydantic import BaseModel

from bff_web.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.authorization import ActorPermissions
from sportapp_platform.errors import ErrorResponse, error_response
from sportapp_platform.tokens import InvalidAccessToken, JwksUnavailable, VerifiedAccessToken

ATHLETE = "athlete"
BEARER_PREFIX = "bearer "
UNSUPPORTED_ACTOR_TYPE = "unsupported_actor_type"
_UNAVAILABLE = "No fue posible evaluar las capacidades. Intente de nuevo."

_logger = logging.getLogger("sportapp.bff")

SubscriptionStatus = Literal["ACTIVE", "PROVISIONING", "NOT_APPLICABLE"]


class CapabilitiesResponse(BaseModel):
    actor_type: Literal["athlete", "organizer", "service_provider"]
    subscription_status: SubscriptionStatus
    plan_code: str | None
    plan_version: int | None
    catalog_version: str | None
    authorization_policy_version: str
    permissions: list[str]
    capabilities: list[str]
    evaluated_at: datetime


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.lower().startswith(BEARER_PREFIX):
        return None
    token = authorization[len(BEARER_PREFIX) :].strip()
    return token or None


def _unauthenticated() -> Response:
    response = error_response(
        HTTPStatus.UNAUTHORIZED, InvalidAccessToken.code, "La sesión no es válida."
    )
    response.headers["WWW-Authenticate"] = "Bearer"
    return response


def _unavailable() -> Response:
    return error_response(HTTPStatus.SERVICE_UNAVAILABLE, UpstreamUnavailable.code, _UNAVAILABLE)


def build_capabilities_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    permissions: ActorPermissions,
    athlete_snapshot: Callable[[UUID], dict[str, Any]],
    channel: str,
) -> APIRouter:
    router = APIRouter(prefix="/v1/me", tags=["sessions"])

    @router.get(
        "/capabilities",
        response_model=CapabilitiesResponse,
        summary="Identidad, permisos por actor y capacidades vigentes (HU003)",
        responses={
            HTTPStatus.UNAUTHORIZED.value: {
                "model": ErrorResponse,
                "description": "`invalid_access_token`: falta el token o no es válido.",
            },
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": f"`{UNSUPPORTED_ACTOR_TYPE}`: actor ausente o desconocido.",
            },
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": "`upstream_unavailable`: no se pudo evaluar la identidad o el plan.",
            },
        },
    )
    def get_capabilities(
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | CapabilitiesResponse:
        token = _bearer_token(authorization)
        if token is None:
            return _unauthenticated()
        try:
            identity = verify_token(token)
            user_id = UUID(identity.subject)
        except (InvalidAccessToken, ValueError):
            return _unauthenticated()
        except JwksUnavailable:
            _logger.warning("bff.jwks_unavailable")
            return _unavailable()

        actor = identity.actor_type
        if actor is None or not permissions.is_known_actor(actor):
            _logger.warning(
                "bff.unsupported_actor_type", extra={"fields": {"user_id": str(user_id)}}
            )
            return error_response(
                HTTPStatus.FORBIDDEN, UNSUPPORTED_ACTOR_TYPE, "Tipo de usuario no admitido."
            )

        status: SubscriptionStatus = "NOT_APPLICABLE"
        plan: dict[str, Any] = {}
        capabilities: list[str] = []
        if actor == ATHLETE:
            try:
                snapshot = athlete_snapshot(user_id)
            except (UpstreamTimeout, UpstreamUnavailable):
                _logger.warning(
                    "bff.capabilities_unavailable", extra={"fields": {"user_id": str(user_id)}}
                )
                return _unavailable()
            if snapshot.get("subscription_status") == "ACTIVE":
                status = "ACTIVE"
                plan = snapshot
                capabilities = list(snapshot.get("capabilities", []))
            else:
                # Sin suscripción activa todavía: alta gratuita en curso (ADR-024).
                status = "PROVISIONING"

        response.headers["Cache-Control"] = "no-store"
        return CapabilitiesResponse(
            actor_type=actor,
            subscription_status=status,
            plan_code=plan.get("plan_code"),
            plan_version=plan.get("plan_version"),
            catalog_version=plan.get("catalog_version"),
            authorization_policy_version=permissions.version,
            permissions=list(permissions.for_actor(actor, channel)),
            capabilities=capabilities,
            evaluated_at=datetime.now(UTC),
        )

    return router
