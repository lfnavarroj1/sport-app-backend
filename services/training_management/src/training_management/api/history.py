"""HU011: `GET /v1/training-history` y `GET /v1/athletes/{athlete_id}/training-history`.

Contrato aprobado el 2026-10-12. El primero devuelve el historial propio del
deportista autenticado; el segundo, el de un deportista que autorizó al profesional
autenticado. Ambos leen la base de lectura y paginan por cursor opaco.
"""

from datetime import date, datetime
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
from training_management.application.errors import (
    AthleteAuthorizationRequired,
    PermissionRequired,
)
from training_management.application.queries.training_history import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    GetTrainingHistory,
    HistoryFilters,
    HistoryPage,
    InvalidHistoryQuery,
)
from training_management.domain.dashboard.model import Source
from training_management.domain.plans.model import Sport


class SessionHistoryItem(BaseModel):
    session_id: UUID
    plan_id: UUID | None
    sport: Sport
    started_at: datetime
    ended_at: datetime
    duration_seconds: int
    # HU031 (ADR-042 §12): campo nuevo compatible; no se exponen `external_ref` ni distancia.
    source: Source = Field(
        description="Origen: `SPORTAPP` (registrada en SportApp) o `STRAVA` (importada, HU031)."
    )


class HistoryFiltersResponse(BaseModel):
    sports: list[Sport] = Field(description="Deportes disponibles para filtrar.")


class TrainingHistoryResponse(BaseModel):
    items: list[SessionHistoryItem]
    next_cursor: str | None = Field(
        description="Cursor opaco de la página siguiente; `null` si no hay más."
    )
    filters: HistoryFiltersResponse

    @classmethod
    def from_page(cls, page: HistoryPage) -> "TrainingHistoryResponse":
        return cls(
            items=[
                SessionHistoryItem(
                    session_id=item.session_id,
                    plan_id=item.plan_id,
                    sport=Sport(item.sport),
                    started_at=item.started_at,
                    ended_at=item.ended_at,
                    duration_seconds=item.duration_seconds,
                    source=Source(item.source),
                )
                for item in page.items
            ],
            next_cursor=page.next_cursor,
            filters=HistoryFiltersResponse(
                sports=[Sport(sport) for sport in page.available_sports]
            ),
        )


_RESPONSES = {
    **AUTH_RESPONSES,
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required` o `athlete_authorization_required` (CA3).",
    },
    HTTPStatus.UNPROCESSABLE_ENTITY.value: {
        "model": ErrorResponse,
        "description": "`validation_error`: rango de fechas, límite o cursor inválidos.",
    },
}


def _invalid(error: InvalidHistoryQuery) -> Response:
    return error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        error.code,
        "La consulta no es válida.",
        [ErrorDetail(field=f"query.{error.field}", issue=error.issue)],
    )


def build_history_router(
    *, verify_token: TokenVerifier, get_history: GetTrainingHistory
) -> APIRouter:
    router = APIRouter(tags=["training-history"])

    def filters(
        date_from: date | None,
        date_to: date | None,
        sport: Sport | None,
        limit: int,
        cursor: str | None,
    ) -> HistoryFilters:
        return HistoryFilters(
            date_from=date_from, date_to=date_to, sport=sport, limit=limit, cursor=cursor
        )

    def respond(page: HistoryPage, response: Response) -> TrainingHistoryResponse:
        response.headers["Cache-Control"] = "no-store"
        return TrainingHistoryResponse.from_page(page)

    @router.get(
        "/v1/training-history",
        response_model=TrainingHistoryResponse,
        summary="Mi historial de entrenamiento (HU011)",
        responses=_RESPONSES,
    )
    def own_history(
        *,
        response: Response,
        date_from: Annotated[date | None, Query(alias="from")] = None,
        date_to: Annotated[date | None, Query(alias="to")] = None,
        sport: Sport | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | TrainingHistoryResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            page = get_history.own(caller, filters(date_from, date_to, sport, limit, cursor))
        except PermissionRequired as error:
            return forbidden(error)
        except InvalidHistoryQuery as error:
            return _invalid(error)
        return respond(page, response)

    @router.get(
        "/v1/athletes/{athlete_id}/training-history",
        response_model=TrainingHistoryResponse,
        summary="Historial de entrenamiento de un deportista que me autorizó (HU011)",
        responses=_RESPONSES,
    )
    def athlete_history(
        *,
        athlete_id: UUID,
        response: Response,
        date_from: Annotated[date | None, Query(alias="from")] = None,
        date_to: Annotated[date | None, Query(alias="to")] = None,
        sport: Sport | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | TrainingHistoryResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            page = get_history.of_athlete(
                caller, athlete_id, filters(date_from, date_to, sport, limit, cursor)
            )
        except (PermissionRequired, AthleteAuthorizationRequired) as error:
            return forbidden(error)
        except InvalidHistoryQuery as error:
            return _invalid(error)
        return respond(page, response)

    return router
