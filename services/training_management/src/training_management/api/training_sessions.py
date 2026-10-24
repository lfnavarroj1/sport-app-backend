"""HU032: sesiones de entrenamiento (contrato de ADR-029).

- `POST /v1/training-sessions`: inicia (`201` + `Location`).
- `POST /v1/training-sessions/{session_id}/finish`: finaliza (`200`); HU036 agrega el motivo
  opcional `reason: RISK_STOP` (ADR-044 §10), que la respuesta devuelve en `end_reason`.
- `GET /v1/training-sessions/active`: sesión activa proyectada (`200`) o `204`.

Los cuerpos se leen como JSON sin validación automática para responder el catálogo de
ADR-029 (`details[{field, issue}]`); su esquema se documenta en el OpenAPI. El deportista
sale del token. Los instantes se responden en UTC con sufijo `Z`.
"""

from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Header, Response
from pydantic import BaseModel, ConfigDict, Field

from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from training_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    correlation_uuid,
    forbidden,
)
from training_management.api.session_payload import parse_finish, parse_start
from training_management.application.commands.training_sessions import (
    FinishTrainingSession,
    FinishTrainingSessionCommand,
    SessionResult,
    StartTrainingSession,
    StartTrainingSessionCommand,
)
from training_management.application.errors import (
    ActiveSessionExists,
    CapabilitiesTimeout,
    CapabilitiesUnavailable,
    CapabilityRequired,
    IdempotencyKeyReused,
    PermissionRequired,
    SessionIdReused,
    TrainingPlanNotFound,
    TrainingSessionAlreadyFinished,
    TrainingSessionNotFound,
)
from training_management.application.queries.active_session import GetActiveTrainingSession
from training_management.application.session_documents import session_document
from training_management.domain.plans.model import Sport
from training_management.domain.sessions.model import SessionEndReason
from training_management.domain.sessions.validation import (
    InvalidTrainingSession,
    Issue,
    Violation,
)

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
INSTANT = "Instante UTC con sufijo `Z` y precisión de segundos (`YYYY-MM-DDTHH:MM:SSZ`)."

_ERRORS: dict[type[Exception], tuple[HTTPStatus, str]] = {
    TrainingPlanNotFound: (
        HTTPStatus.NOT_FOUND,
        "El plan no existe, no es suyo o no incluye ese deporte.",
    ),
    TrainingSessionNotFound: (HTTPStatus.NOT_FOUND, "La sesión de entrenamiento no existe."),
    ActiveSessionExists: (HTTPStatus.CONFLICT, "Ya tiene una sesión de entrenamiento activa."),
    SessionIdReused: (HTTPStatus.CONFLICT, "El identificador de sesión ya se usó con otros datos."),
    TrainingSessionAlreadyFinished: (
        HTTPStatus.CONFLICT,
        "La sesión ya terminó con otro instante final o motivo.",
    ),
    IdempotencyKeyReused: (
        HTTPStatus.CONFLICT,
        "La clave de idempotencia ya se usó con otra solicitud.",
    ),
    CapabilitiesTimeout: (
        HTTPStatus.GATEWAY_TIMEOUT,
        "No fue posible verificar su plan a tiempo. Intente de nuevo.",
    ),
    CapabilitiesUnavailable: (
        HTTPStatus.SERVICE_UNAVAILABLE,
        "No fue posible verificar su plan. Intente de nuevo.",
    ),
}


class StartTrainingSessionRequest(BaseModel):
    """Esquema documentado del inicio; la validación la hacen el transporte y el dominio."""

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
            description="HU036: `RISK_STOP` cuando el deportista detiene la práctica tras una "
            "alerta de riesgo; opcional."
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
    return schema


def _body(model: type[BaseModel]) -> dict[str, Any]:
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _schema(model)}},
        }
    }


def _invalid(violations: list[Violation] | tuple[Violation, ...]) -> Response:
    return error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        InvalidTrainingSession.code,
        "La solicitud no es válida.",
        [ErrorDetail(field=item.field, issue=item.issue.value) for item in violations],
    )


def _idempotency_key(raw: str | None) -> tuple[UUID | None, Violation | None]:
    if raw is None or not raw.strip():
        return None, Violation(IDEMPOTENCY_HEADER, Issue.REQUIRED)
    try:
        return UUID(raw.strip()), None
    except ValueError:
        return None, Violation(IDEMPOTENCY_HEADER, Issue.INVALID_UUID)


def _failure(error: Exception) -> Response:
    if isinstance(error, PermissionRequired | CapabilityRequired):
        return forbidden(error)
    if isinstance(error, InvalidTrainingSession):
        return _invalid(error.violations)
    status, message = _ERRORS[type(error)]
    return error_response(status, getattr(error, "code", ""), message)


def _session_response(response: Response, result: SessionResult) -> TrainingSessionResponse:
    if result.replayed:
        response.headers[REPLAYED_HEADER] = "true"
    return TrainingSessionResponse.model_validate(session_document(result.session))


_HANDLED: tuple[type[Exception], ...] = (
    PermissionRequired,
    CapabilityRequired,
    InvalidTrainingSession,
    *_ERRORS,
)

_REPLAY_HEADERS = {
    REPLAYED_HEADER: {
        "description": "`true` cuando se devuelve un resultado ya confirmado.",
        "schema": {"type": "string"},
    }
}
_FORBIDDEN = {
    "model": ErrorResponse,
    "description": "`permission_required` (no es deportista) o `capability_required` "
    "(`training.session.record`, solo al iniciar).",
}
_INVALID = {
    "model": ErrorResponse,
    "description": "`validation_error` con `details[{field, issue}]` (ADR-029).",
}


def build_training_sessions_router(
    *,
    verify_token: TokenVerifier,
    start: StartTrainingSession,
    finish: FinishTrainingSession,
    get_active: GetActiveTrainingSession,
) -> APIRouter:
    router = APIRouter(prefix="/v1/training-sessions", tags=["training-sessions"])

    @router.post(
        "",
        status_code=HTTPStatus.CREATED,
        response_model=TrainingSessionResponse,
        summary="Iniciar una sesión de entrenamiento (HU032)",
        openapi_extra=_body(StartTrainingSessionRequest),
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.CREATED.value: {
                "headers": {"Location": {"schema": {"type": "string"}}, **_REPLAY_HEADERS}
            },
            HTTPStatus.FORBIDDEN.value: _FORBIDDEN,
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": "`training_plan_not_found`.",
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`active_session_exists`, `session_id_reused` o "
                "`idempotency_key_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: _INVALID,
            HTTPStatus.GATEWAY_TIMEOUT.value: {
                "model": ErrorResponse,
                "description": "`upstream_timeout`: Afiliación no respondió a tiempo.",
            },
        },
    )
    def start_session(
        response: Response,
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | TrainingSessionResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        raw = parse_start(body)
        key, key_problem = _idempotency_key(idempotency_key)
        try:
            if key is None:
                # La clave es inválida: se informan también las reglas del inicio.
                problems = [key_problem] if key_problem else []
                try:
                    start.validate(caller, raw)
                except InvalidTrainingSession as error:
                    problems.extend(error.violations)
                return _invalid(problems)
            result = start(
                caller,
                StartTrainingSessionCommand(
                    start=raw, idempotency_key=key, correlation_id=correlation_uuid()
                ),
            )
        except _HANDLED as error:
            return _failure(error)
        response.headers["Location"] = f"/v1/training-sessions/{result.session.session_id}"
        return _session_response(response, result)

    @router.post(
        "/{session_id}/finish",
        response_model=TrainingSessionResponse,
        summary="Finalizar una sesión de entrenamiento propia (HU032)",
        openapi_extra=_body(FinishTrainingSessionRequest),
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.OK.value: {"headers": _REPLAY_HEADERS},
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` (no es deportista).",
            },
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": "`training_session_not_found`: inexistente o ajena.",
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`training_session_already_finished` (ya terminó con otro "
                "`ended_at` u otro `reason`) o `idempotency_key_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: _INVALID,
        },
    )
    def finish_session(
        session_id: str,
        response: Response,
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | TrainingSessionResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        ended_at, reason, transport = parse_finish(body)
        key, key_problem = _idempotency_key(idempotency_key)
        command = FinishTrainingSessionCommand(
            session_id=session_id,
            ended_at=ended_at,
            reason=reason,
            idempotency_key=key or UUID(int=0),
            transport_violations=transport,
            correlation_id=correlation_uuid(),
        )
        try:
            if key is None:
                problems = [key_problem] if key_problem else []
                try:
                    finish.validate(caller, command)
                except InvalidTrainingSession as error:
                    problems.extend(error.violations)
                return _invalid(problems)
            result = finish(caller, command)
        except _HANDLED as error:
            return _failure(error)
        return _session_response(response, result)

    @router.get(
        "/active",
        response_model=ActiveTrainingSessionResponse,
        summary="Sesión de entrenamiento activa propia (HU032, lectura CQRS)",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.NO_CONTENT.value: {"description": "No hay sesión activa proyectada."},
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` (no es deportista).",
            },
        },
    )
    def active_session(
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | ActiveTrainingSessionResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            view = get_active(caller)
        except PermissionRequired as error:
            return forbidden(error)
        if view is None:
            return Response(status_code=HTTPStatus.NO_CONTENT)
        response.headers["Cache-Control"] = "no-store"
        return ActiveTrainingSessionResponse(
            session_id=view.session_id,
            athlete_id=view.athlete_id,
            plan_id=view.plan_id,
            sport=Sport(view.sport),
            started_at=view.started_at,
            status="ACTIVE",
            version=view.version,
        )

    return router
