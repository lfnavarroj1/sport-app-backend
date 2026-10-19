"""HU035: `GET /v1/me/training-plan-sessions` (contrato interno de ADR-031).

Lo consume `bff-mobile` para componer el calendario del deportista. No expone objetivos
completos, `professional_id` ni datos de perfil.
"""

from datetime import date
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, Field

from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from training_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    forbidden,
)
from training_management.application.errors import PermissionRequired
from training_management.application.queries.planned_sessions import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    GetPlannedSessions,
    InvalidPlannedSessionQuery,
    PlannedSessionFilters,
    PlannedSessionPage,
)
from training_management.domain.plans.model import Sport


class PlannedSessionItem(BaseModel):
    plan_id: UUID
    plan_title: str
    session_id: Annotated[
        UUID, Field(description="Identificador estable de la sesión dentro del plan.")
    ]
    scheduled_date: date
    sport: Sport
    duration_minutes: int
    rpe: Annotated[int, Field(description="Intensidad percibida RPE, escala CR10 (1 a 10).")]


class PlannedSessionsResponse(BaseModel):
    items: list[PlannedSessionItem]
    next_cursor: str | None = Field(
        description="Cursor opaco de la página siguiente; `null` si no hay más."
    )

    @classmethod
    def from_page(cls, page: PlannedSessionPage) -> "PlannedSessionsResponse":
        return cls(
            items=[
                PlannedSessionItem(
                    plan_id=item.plan_id,
                    plan_title=item.plan_title,
                    session_id=item.session_id,
                    scheduled_date=item.scheduled_date,
                    sport=Sport(item.sport),
                    duration_minutes=item.duration_minutes,
                    rpe=item.intensity_rpe,
                )
                for item in page.items
            ],
            next_cursor=page.next_cursor,
        )


def build_planned_sessions_router(
    *, verify_token: TokenVerifier, get_sessions: GetPlannedSessions
) -> APIRouter:
    router = APIRouter(tags=["training-plan-sessions"])

    @router.get(
        "/v1/me/training-plan-sessions",
        response_model=PlannedSessionsResponse,
        summary="Mis sesiones planificadas por fecha (HU035, interno)",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required`: solo el deportista.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: fechas, rango, límite o cursor inválidos.",
            },
        },
    )
    def planned_sessions(
        *,
        response: Response,
        date_from: Annotated[date, Query(alias="from", description="Fecha inclusiva.")],
        date_to: Annotated[date, Query(alias="to", description="Fecha inclusiva.")],
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | PlannedSessionsResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            page = get_sessions.own(
                caller,
                PlannedSessionFilters(
                    date_from=date_from, date_to=date_to, limit=limit, cursor=cursor
                ),
            )
        except PermissionRequired as error:
            return forbidden(error)
        except InvalidPlannedSessionQuery as error:
            return error_response(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                error.code,
                "La consulta no es válida.",
                [ErrorDetail(field=f"query.{error.field}", issue=error.issue)],
            )
        response.headers["Cache-Control"] = "no-store"
        return PlannedSessionsResponse.from_page(page)

    return router
