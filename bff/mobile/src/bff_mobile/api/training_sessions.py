"""HU032 en el canal móvil: sesiones de entrenamiento (contrato de ADR-029).

El BFF autentica (JWKS), aplica el filtro de borde por actor (`athlete`) y reenvía cuerpo,
clave de idempotencia y correlación sin reinterpretarlos: el deportista sale del token,
y las reglas (capacidad, plan, transiciones, ventanas de tiempo, idempotencia) y sus
errores los decide `training_management` y se entregan sin alterar. La app reintenta
solo ante pérdida de red, `502`, `503` o `504`, con la misma clave (ADR-026, ADR-029).
HU036 (ADR-044 §10): la finalización acepta `reason: RISK_STOP` («Detener la práctica») y la
sesión lo devuelve en `end_reason`.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Body, Header, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from bff_mobile.clients.training_management import TrainingManagementClient
from bff_mobile.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.errors import ErrorResponse, error_response
from sportapp_platform.tokens import InvalidAccessToken, JwksUnavailable, VerifiedAccessToken

ATHLETE = "athlete"
BEARER_PREFIX = "bearer "
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
PERMISSION_REQUIRED = "permission_required"
INSTANT = "Instante UTC con sufijo `Z` y precisión de segundos (`YYYY-MM-DDTHH:MM:SSZ`)."

_logger = logging.getLogger("sportapp.bff")

Sport = Literal["running", "cycling", "swimming", "strength", "other"]


class SessionEndReason(StrEnum):
    """HU036: motivo de la finalización (catálogo del servicio; el BFF solo lo documenta)."""

    RISK_STOP = "RISK_STOP"


class StartTrainingSessionRequest(BaseModel):
    """Esquema documentado del inicio; la validación la hace el servicio (ADR-029)."""

    model_config = ConfigDict(extra="forbid")

    session_id: Annotated[UUID, Field(description="UUID v4 generado por el móvil.")]
    plan_id: Annotated[
        UUID | None,
        Field(description="Plan propio `ASSIGNED` que contiene el deporte; opcional."),
    ] = None
    sport: Sport
    started_at: Annotated[datetime, Field(description=INSTANT, examples=["2026-10-12T12:30:00Z"])]


class FinishTrainingSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ended_at: Annotated[datetime, Field(description=INSTANT, examples=["2026-10-12T13:15:00Z"])]
    reason: Annotated[
        SessionEndReason | None,
        Field(
            description="HU036: `RISK_STOP` al detener la práctica tras una alerta de riesgo; "
            "opcional."
        ),
    ] = None


class TrainingSessionResponse(BaseModel):
    """Sesión confirmada; `ended_at` y `duration_seconds` son `null` mientras está activa."""

    session_id: UUID
    athlete_id: UUID
    plan_id: UUID | None
    sport: Sport
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: Annotated[
        int | None, Field(description="Calculada por el backend: `ended_at` - `started_at`.")
    ]
    status: Literal["ACTIVE", "FINISHED"]
    version: int
    created_at: datetime
    updated_at: datetime
    end_reason: Annotated[
        SessionEndReason | None,
        Field(description="Motivo de la finalización (HU036); `null` si no se indicó."),
    ] = None


class ActiveTrainingSessionResponse(BaseModel):
    """Sesión activa proyectada (lectura eventual)."""

    session_id: UUID
    athlete_id: UUID
    plan_id: UUID | None
    sport: Sport
    started_at: datetime
    status: Literal["ACTIVE"]
    version: int


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    schema.pop("$defs", None)
    return {"requestBody": {"required": True, "content": {"application/json": {"schema": schema}}}}


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


def _passthrough(upstream: httpx.Response, allowed: set[int]) -> Response:
    if upstream.status_code not in allowed:
        _logger.warning(
            "bff.upstream_unexpected_status",
            extra={"fields": {"upstream": "training-management", "status": upstream.status_code}},
        )
        return _upstream_failure(UpstreamUnavailable())
    headers = {
        name: upstream.headers[name]
        for name in ("Location", REPLAYED_HEADER, "Cache-Control")
        if name in upstream.headers
    }
    if upstream.status_code == HTTPStatus.NO_CONTENT:
        return Response(status_code=HTTPStatus.NO_CONTENT, headers=headers)
    return JSONResponse(status_code=upstream.status_code, content=upstream.json(), headers=headers)


_ERRORS: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNAUTHORIZED.value: {
        "model": ErrorResponse,
        "description": "`invalid_access_token`: falta el token o no es válido.",
    },
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required` (no es deportista) o `capability_required` "
        "(`training.session.record`, solo al iniciar).",
    },
    HTTPStatus.SERVICE_UNAVAILABLE.value: {
        "model": ErrorResponse,
        "description": "`upstream_unavailable`.",
    },
    HTTPStatus.GATEWAY_TIMEOUT.value: {
        "model": ErrorResponse,
        "description": "`upstream_timeout`.",
    },
}
_COMMAND_ERRORS: dict[int | str, dict[str, Any]] = {
    HTTPStatus.NOT_FOUND.value: {
        "model": ErrorResponse,
        "description": "`training_plan_not_found` o `training_session_not_found`.",
    },
    HTTPStatus.CONFLICT.value: {
        "model": ErrorResponse,
        "description": "`active_session_exists`, `session_id_reused`, "
        "`training_session_already_finished` o `idempotency_key_reused`.",
    },
    HTTPStatus.UNPROCESSABLE_ENTITY.value: {
        "model": ErrorResponse,
        "description": "`validation_error` con `details[{field, issue}]` (ADR-029).",
    },
}
_REPLAY = {
    REPLAYED_HEADER: {
        "description": "`true` cuando se devuelve un resultado ya confirmado.",
        "schema": {"type": "string"},
    }
}
_COMMAND_STATUSES = {
    HTTPStatus.FORBIDDEN,
    HTTPStatus.NOT_FOUND,
    HTTPStatus.CONFLICT,
    HTTPStatus.UNPROCESSABLE_ENTITY,
    HTTPStatus.SERVICE_UNAVAILABLE,
    HTTPStatus.GATEWAY_TIMEOUT,
}


def build_training_sessions_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    training: TrainingManagementClient,
) -> APIRouter:
    router = APIRouter(prefix="/v1/training-sessions", tags=["training-sessions"])

    def athlete_token(authorization: str | None) -> str | Response:
        if not authorization or not authorization.lower().startswith(BEARER_PREFIX):
            return _unauthenticated()
        token = authorization[len(BEARER_PREFIX) :].strip()
        try:
            identity = verify_token(token)
        except InvalidAccessToken:
            return _unauthenticated()
        except JwksUnavailable:
            _logger.warning("bff.jwks_unavailable")
            return _upstream_failure(UpstreamUnavailable())
        if identity.actor_type != ATHLETE:
            return error_response(
                HTTPStatus.FORBIDDEN,
                PERMISSION_REQUIRED,
                "Su tipo de usuario no puede realizar esta operación.",
            )
        return token

    def relay(send: Callable[[], httpx.Response], allowed: set[int]) -> Response:
        try:
            upstream = send()
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        return _passthrough(upstream, allowed)

    @router.post(
        "",
        status_code=HTTPStatus.CREATED,
        response_model=TrainingSessionResponse,
        summary="Iniciar una sesión de entrenamiento (HU032)",
        openapi_extra=_schema(StartTrainingSessionRequest),
        responses={
            **_ERRORS,
            **_COMMAND_ERRORS,
            HTTPStatus.CREATED.value: {
                "headers": {"Location": {"schema": {"type": "string"}}, **_REPLAY}
            },
        },
    )
    def start_session(
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = athlete_token(authorization)
        if isinstance(token, Response):
            return token
        return relay(
            lambda: training.start_session(token, body, idempotency_key),
            {HTTPStatus.CREATED, *_COMMAND_STATUSES},
        )

    @router.post(
        "/{session_id}/finish",
        response_model=TrainingSessionResponse,
        summary="Finalizar una sesión de entrenamiento propia (HU032)",
        openapi_extra=_schema(FinishTrainingSessionRequest),
        responses={**_ERRORS, **_COMMAND_ERRORS, HTTPStatus.OK.value: {"headers": _REPLAY}},
    )
    def finish_session(
        session_id: str,
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = athlete_token(authorization)
        if isinstance(token, Response):
            return token
        return relay(
            lambda: training.finish_session(token, session_id, body, idempotency_key),
            {HTTPStatus.OK, *_COMMAND_STATUSES},
        )

    @router.get(
        "/active",
        response_model=ActiveTrainingSessionResponse,
        summary="Sesión de entrenamiento activa propia (HU032, lectura CQRS)",
        responses={
            **_ERRORS,
            HTTPStatus.NO_CONTENT.value: {"description": "No hay sesión activa proyectada."},
        },
    )
    def active_session(authorization: Annotated[str | None, Header()] = None) -> Response:
        token = athlete_token(authorization)
        if isinstance(token, Response):
            return token
        return relay(
            lambda: training.active_session(token),
            {HTTPStatus.OK, HTTPStatus.NO_CONTENT, HTTPStatus.FORBIDDEN},
        )

    return router
