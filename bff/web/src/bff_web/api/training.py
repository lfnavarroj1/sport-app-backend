"""HU010 en el canal web: autorización del deportista y planes de entrenamiento.

Contrato aprobado el 2026-10-12. El BFF autentica (JWKS), aplica el filtro de
borde por actor/permiso, resuelve los correos con el contrato interno de Usuarios
y reenvía al servicio propietario con el token del usuario; las reglas
autoritativas (capacidad, autorización, coherencia, idempotencia) viven en
`training_management` y sus errores se entregan sin alterar.

Para no revelar si un correo pertenece a un deportista, la asignación responde
`403 athlete_authorization_required` tanto si el correo no existe como si el
deportista no autorizó al profesional.
"""

import logging
from collections.abc import Callable
from datetime import date, datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

from bff_web.clients.training_management import TrainingManagementClient
from bff_web.clients.users_management import (
    UpstreamTimeout,
    UpstreamUnavailable,
    UsersManagementClient,
)
from sportapp_platform.authorization import ActorPermissions
from sportapp_platform.errors import ErrorResponse, error_response
from sportapp_platform.tokens import InvalidAccessToken, JwksUnavailable, VerifiedAccessToken

ATHLETE = "athlete"
SERVICE_PROVIDER = "service_provider"
PLAN_AUTHOR_PERMISSION = "training.plan.author"
BEARER_PREFIX = "bearer "
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
PERMISSION_REQUIRED = "permission_required"
PROFESSIONAL_NOT_FOUND = "professional_not_found"
ATHLETE_AUTHORIZATION_REQUIRED = "athlete_authorization_required"

_logger = logging.getLogger("sportapp.bff")

Sport = Literal["running", "cycling", "swimming", "strength", "other"]
Objective = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class GrantAuthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    professional_email: Annotated[EmailStr, Field(max_length=254)]


class TrainingAuthorizationResponse(BaseModel):
    athlete_id: UUID
    professional_id: UUID
    status: Literal["ACTIVE"]
    granted_at: datetime


class PlannedSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled_date: date
    sport: Sport
    duration_minutes: Annotated[int, Field(ge=1, le=600)]
    intensity_rpe: Annotated[
        int, Field(ge=1, le=10, description="Intensidad percibida RPE, escala CR10 (1 a 10).")
    ]


class CreateTrainingPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    athlete_email: Annotated[EmailStr, Field(max_length=254)]
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    objectives: Annotated[list[Objective], Field(min_length=1, max_length=5)]
    start_date: date
    end_date: date
    sessions: Annotated[list[PlannedSession], Field(min_length=1, max_length=100)]


class TrainingPlanResponse(BaseModel):
    plan_id: UUID
    athlete_id: UUID
    professional_id: UUID
    title: str
    objectives: list[str]
    start_date: date
    end_date: date
    status: Literal["ASSIGNED"]
    version: int
    sessions: list[PlannedSession]
    created_at: datetime


_UNAUTHORIZED: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNAUTHORIZED.value: {
        "model": ErrorResponse,
        "description": "`invalid_access_token`: falta el token o no es válido.",
    }
}
_UPSTREAM: dict[int | str, dict[str, Any]] = {
    HTTPStatus.SERVICE_UNAVAILABLE.value: {
        "model": ErrorResponse,
        "description": "`upstream_unavailable`.",
    },
    HTTPStatus.GATEWAY_TIMEOUT.value: {
        "model": ErrorResponse,
        "description": "`upstream_timeout`.",
    },
}


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


def _upstream_failure(error: UpstreamTimeout | UpstreamUnavailable) -> Response:
    if isinstance(error, UpstreamTimeout):
        return error_response(
            HTTPStatus.GATEWAY_TIMEOUT, error.code, "Un servicio interno no respondió."
        )
    return error_response(
        HTTPStatus.SERVICE_UNAVAILABLE, error.code, "Un servicio interno no está disponible."
    )


def _permission_required() -> Response:
    return error_response(
        HTTPStatus.FORBIDDEN,
        PERMISSION_REQUIRED,
        "Su tipo de usuario no puede realizar esta operación.",
    )


def _passthrough(
    upstream: Any,
    allowed: set[int],
    headers: tuple[str, ...] = (),
    *,
    source: str = "training-management",
) -> Response:
    if upstream.status_code not in allowed:
        _logger.warning(
            "bff.upstream_unexpected_status",
            extra={"fields": {"upstream": source, "status": upstream.status_code}},
        )
        return _upstream_failure(UpstreamUnavailable())
    forwarded = {name: upstream.headers[name] for name in headers if name in upstream.headers}
    if upstream.status_code == HTTPStatus.NO_CONTENT:
        return Response(status_code=HTTPStatus.NO_CONTENT, headers=forwarded)
    return JSONResponse(
        status_code=upstream.status_code, content=upstream.json(), headers=forwarded
    )


class _Session:
    """Token verificado del usuario: se reenvía tal cual al servicio propietario."""

    def __init__(self, token: str, identity: VerifiedAccessToken) -> None:
        self.token = token
        self.actor_type = identity.actor_type


def authenticate_session(
    verify_token: Callable[[str], VerifiedAccessToken], authorization: str | None
) -> "_Session | Response":
    """Token verificado del usuario, o la respuesta de error del BFF."""
    token = _bearer_token(authorization)
    if token is None:
        return _unauthenticated()
    try:
        identity = verify_token(token)
    except InvalidAccessToken:
        return _unauthenticated()
    except JwksUnavailable:
        _logger.warning("bff.jwks_unavailable")
        return _upstream_failure(UpstreamUnavailable())
    return _Session(token, identity)


def build_training_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    permissions: ActorPermissions,
    users: UsersManagementClient,
    training: TrainingManagementClient,
    channel: str,
) -> APIRouter:
    router = APIRouter(tags=["training"])

    def authenticate(authorization: str | None) -> _Session | Response:
        return authenticate_session(verify_token, authorization)

    @router.post(
        "/v1/me/training-authorizations",
        response_model=TrainingAuthorizationResponse,
        summary="Autorizar a un profesional para asignarme planes (HU010)",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            HTTPStatus.CREATED.value: {
                "model": TrainingAuthorizationResponse,
                "description": "Autorización creada o reactivada (`200` si ya estaba activa).",
            },
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` (no es deportista) o "
                "`capability_required` (`training.plan.manage`).",
            },
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": f"`{PROFESSIONAL_NOT_FOUND}`: el correo no es de un profesional.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`.",
            },
        },
    )
    def grant_authorization(
        body: GrantAuthorizationRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = authenticate(authorization)
        if isinstance(session, Response):
            return session
        if session.actor_type != ATHLETE:
            return _permission_required()
        try:
            professional = users.lookup_user(str(body.professional_email))
            if professional is None or professional.get("actor_type") != SERVICE_PROVIDER:
                return error_response(
                    HTTPStatus.NOT_FOUND,
                    PROFESSIONAL_NOT_FOUND,
                    "No existe un profesional con ese correo.",
                )
            upstream = training.grant_authorization(
                session.token, UUID(str(professional["user_id"]))
            )
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        return _passthrough(
            upstream,
            {
                HTTPStatus.OK,
                HTTPStatus.CREATED,
                HTTPStatus.FORBIDDEN,
                HTTPStatus.SERVICE_UNAVAILABLE,
            },
        )

    @router.delete(
        "/v1/me/training-authorizations/{professional_id}",
        status_code=HTTPStatus.NO_CONTENT,
        summary="Revocar la autorización de un profesional (HU010)",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required`: solo el deportista revoca.",
            },
        },
    )
    def revoke_authorization(
        professional_id: UUID,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = authenticate(authorization)
        if isinstance(session, Response):
            return session
        if session.actor_type != ATHLETE:
            return _permission_required()
        try:
            upstream = training.revoke_authorization(session.token, professional_id)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        return _passthrough(upstream, {HTTPStatus.NO_CONTENT, HTTPStatus.FORBIDDEN})

    @router.post(
        "/v1/training-plans",
        status_code=HTTPStatus.CREATED,
        response_model=TrainingPlanResponse,
        summary="Crear y asignar un plan de entrenamiento (HU010)",
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
                "description": "`permission_required` (`training.plan.author`) o "
                f"`{ATHLETE_AUTHORIZATION_REQUIRED}` (correo inexistente o sin autorización).",
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`idempotency_key_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` o `training_plan_invalid` (reglas en "
                "`details`).",
            },
        },
    )
    def create_plan(
        body: CreateTrainingPlanRequest,
        idempotency_key: Annotated[UUID, Header(alias=IDEMPOTENCY_HEADER)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = authenticate(authorization)
        if isinstance(session, Response):
            return session
        actor = session.actor_type
        if actor is None or PLAN_AUTHOR_PERMISSION not in permissions.for_actor(actor, channel):
            return _permission_required()
        try:
            athlete = users.lookup_user(str(body.athlete_email))
            if athlete is None or athlete.get("actor_type") != ATHLETE:
                return error_response(
                    HTTPStatus.FORBIDDEN,
                    ATHLETE_AUTHORIZATION_REQUIRED,
                    "El deportista no ha autorizado a este profesional para asignarle planes.",
                )
            payload = body.model_dump(mode="json", exclude={"athlete_email"}) | {
                "athlete_id": str(athlete["user_id"])
            }
            upstream = training.create_plan(session.token, payload, idempotency_key)
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
        )

    @router.get(
        "/v1/training-plans/{plan_id}",
        response_model=TrainingPlanResponse,
        summary="Consultar un plan de entrenamiento (HU010)",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` o `capability_required`.",
            },
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": "`training_plan_not_found`: no existe, no es visible o aún no "
                "está proyectado.",
            },
        },
    )
    def get_plan(
        plan_id: UUID,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = authenticate(authorization)
        if isinstance(session, Response):
            return session
        try:
            upstream = training.get_plan(session.token, plan_id)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        response = _passthrough(
            upstream,
            {
                HTTPStatus.OK,
                HTTPStatus.FORBIDDEN,
                HTTPStatus.NOT_FOUND,
                HTTPStatus.SERVICE_UNAVAILABLE,
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    return router
