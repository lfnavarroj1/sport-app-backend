"""HU010: `POST /v1/training-plans` y `GET /v1/training-plans/{plan_id}`.

Contrato aprobado el 2026-10-12. El profesional sale del token; el cuerpo solo
identifica al deportista. La consulta lee la base de lectura y no expone datos
del perfil del deportista (CA4).
"""

from datetime import date, datetime
from http import HTTPStatus
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sportapp_platform.correlation import current_correlation_id
from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from training_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    forbidden,
    unavailable,
)
from training_management.application.commands.create_training_plan import (
    CreateTrainingPlan,
    CreateTrainingPlanCommand,
    SessionInput,
)
from training_management.application.errors import (
    AthleteAuthorizationRequired,
    CapabilitiesUnavailable,
    CapabilityRequired,
    IdempotencyKeyReused,
    PermissionRequired,
    TrainingPlanNotFound,
)
from training_management.application.ports import PlanView
from training_management.application.queries.get_training_plan import GetTrainingPlan
from training_management.domain.plans.errors import InvalidTrainingPlan
from training_management.domain.plans.model import (
    DURATION_MINUTES_MAX,
    DURATION_MINUTES_MIN,
    INTENSITY_RPE_MAX,
    INTENSITY_RPE_MIN,
    OBJECTIVE_MAX_LENGTH,
    OBJECTIVES_MAX,
    PLAN_RULE_ISSUES,
    SESSIONS_MAX,
    TITLE_MAX_LENGTH,
    Sport,
)

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"

Objective = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=OBJECTIVE_MAX_LENGTH)
]


class PlannedSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled_date: date
    sport: Sport
    duration_minutes: Annotated[int, Field(ge=DURATION_MINUTES_MIN, le=DURATION_MINUTES_MAX)]
    intensity_rpe: Annotated[
        int,
        Field(
            ge=INTENSITY_RPE_MIN,
            le=INTENSITY_RPE_MAX,
            description="Intensidad percibida RPE, escala CR10 (1 a 10).",
        ),
    ]


class CreateTrainingPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    athlete_id: UUID
    title: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TITLE_MAX_LENGTH)
    ]
    objectives: Annotated[list[Objective], Field(min_length=1, max_length=OBJECTIVES_MAX)]
    start_date: date
    end_date: date
    sessions: Annotated[list[PlannedSessionRequest], Field(min_length=1, max_length=SESSIONS_MAX)]


class PlannedSessionResponse(BaseModel):
    scheduled_date: date
    sport: Sport
    duration_minutes: int
    intensity_rpe: int


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
    sessions: list[PlannedSessionResponse]
    created_at: datetime

    @classmethod
    def from_view(cls, view: PlanView) -> "TrainingPlanResponse":
        return cls(
            plan_id=view.plan_id,
            athlete_id=view.athlete_id,
            professional_id=view.professional_id,
            title=view.title,
            objectives=list(view.objectives),
            start_date=view.start_date,
            end_date=view.end_date,
            status="ASSIGNED",
            version=view.version,
            sessions=[
                PlannedSessionResponse(
                    scheduled_date=session.scheduled_date,
                    sport=Sport(session.sport),
                    duration_minutes=session.duration_minutes,
                    intensity_rpe=session.intensity_rpe,
                )
                for session in view.sessions
            ],
            created_at=view.created_at,
        )


def _correlation_uuid() -> UUID | None:
    correlation_id = current_correlation_id()
    return UUID(correlation_id) if correlation_id else None


def _invalid_plan(error: InvalidTrainingPlan) -> Response:
    return error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        error.code,
        "El plan no cumple las reglas de coherencia.",
        [ErrorDetail(field=f"body.{item.field}", issue=item.issue) for item in error.violations],
    )


def _not_found() -> Response:
    return error_response(
        HTTPStatus.NOT_FOUND, TrainingPlanNotFound.code, "El plan de entrenamiento no existe."
    )


def build_plans_router(
    *,
    verify_token: TokenVerifier,
    create_plan: CreateTrainingPlan,
    get_plan: GetTrainingPlan,
) -> APIRouter:
    router = APIRouter(prefix="/v1/training-plans", tags=["training-plans"])

    @router.post(
        "",
        status_code=HTTPStatus.CREATED,
        response_model=TrainingPlanResponse,
        summary="Crear y asignar un plan de entrenamiento (HU010)",
        responses={
            **AUTH_RESPONSES,
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
                "`athlete_authorization_required`.",
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`idempotency_key_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` (forma del cuerpo) o `training_plan_invalid` "
                "(reglas de coherencia de CA3) con `details[{field, issue}]`. Motivos estables de "
                "`issue` en `training_plan_invalid`: "
                + ", ".join(f"`{issue}`" for issue in PLAN_RULE_ISSUES)
                + ".",
            },
        },
    )
    def create(
        body: CreateTrainingPlanRequest,
        response: Response,
        idempotency_key: Annotated[UUID, Header(alias=IDEMPOTENCY_HEADER)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | TrainingPlanResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        command = CreateTrainingPlanCommand(
            athlete_id=body.athlete_id,
            title=body.title,
            objectives=tuple(body.objectives),
            start_date=body.start_date,
            end_date=body.end_date,
            sessions=tuple(
                SessionInput(
                    scheduled_date=item.scheduled_date,
                    sport=item.sport,
                    duration_minutes=item.duration_minutes,
                    intensity_rpe=item.intensity_rpe,
                )
                for item in body.sessions
            ),
            idempotency_key=idempotency_key,
            correlation_id=_correlation_uuid(),
        )
        try:
            result = create_plan(caller, command)
        except (PermissionRequired, AthleteAuthorizationRequired) as error:
            return forbidden(error)
        except InvalidTrainingPlan as error:
            return _invalid_plan(error)
        except IdempotencyKeyReused as error:
            return error_response(
                HTTPStatus.CONFLICT,
                error.code,
                "La clave de idempotencia ya se usó con otra solicitud.",
            )

        response.headers["Location"] = f"/v1/training-plans/{result.plan.plan_id}"
        if result.replayed:
            response.headers[REPLAYED_HEADER] = "true"
        return TrainingPlanResponse.from_view(result.plan)

    @router.get(
        "/{plan_id}",
        response_model=TrainingPlanResponse,
        summary="Consultar un plan de entrenamiento (HU010)",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` o `capability_required`.",
            },
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": "`training_plan_not_found`: no existe, no es visible para "
                "quien consulta o aún no está proyectado.",
            },
        },
    )
    def get(
        plan_id: UUID,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | TrainingPlanResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            plan = get_plan(caller, plan_id)
        except TrainingPlanNotFound:
            return _not_found()
        except (PermissionRequired, CapabilityRequired) as error:
            return forbidden(error)
        except CapabilitiesUnavailable:
            return unavailable()
        response.headers["Cache-Control"] = "no-store"
        return TrainingPlanResponse.from_view(plan)

    return router
