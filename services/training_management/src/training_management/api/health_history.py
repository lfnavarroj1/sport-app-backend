"""HU012: `GET /v1/me/health-reports` y `GET /v1/athletes/{athlete_id}/health-reports`.

Contrato de ADR-037. El primero devuelve los reportes de salud propios del deportista
autenticado; el segundo, los de un deportista que autorizó al profesional autenticado. Ambos
leen `health_report_view` y paginan por cursor opaco. Cada reporte expone solo `report_id`,
`type`, `body_area`, `severity`, `occurred_at`, `expected_recovery_days`, `status` y
`session_id`; nunca otros datos del perfil ni diagnósticos. Instantes en UTC con sufijo `Z`.
"""

from datetime import UTC, date, datetime
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
from training_management.application.queries.health_history import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    GetHealthHistory,
    HealthHistoryFilters,
    HealthHistoryPage,
    InvalidHealthHistoryQuery,
)
from training_management.domain.health.model import BodyArea, ReportStatus, ReportType, Severity


class HealthReportHistoryItem(BaseModel):
    report_id: UUID
    type: ReportType
    body_area: BodyArea
    severity: Severity
    occurred_at: datetime = Field(description="Instante UTC con sufijo `Z`.")
    expected_recovery_days: int | None
    status: ReportStatus
    session_id: UUID | None


class HealthReportHistoryResponse(BaseModel):
    items: list[HealthReportHistoryItem] = Field(
        description="Reportes en orden `occurred_at` desc, `report_id` desc; vacía si no hay."
    )
    next_cursor: str | None = Field(
        description="Cursor opaco de la página siguiente; `null` si no hay más."
    )

    @classmethod
    def from_page(cls, page: HealthHistoryPage) -> "HealthReportHistoryResponse":
        return cls(
            items=[
                HealthReportHistoryItem(
                    report_id=item.report_id,
                    type=ReportType(item.type),
                    body_area=BodyArea(item.body_area),
                    severity=Severity(item.severity),
                    occurred_at=item.occurred_at.astimezone(UTC),
                    expected_recovery_days=item.expected_recovery_days,
                    status=ReportStatus(item.status),
                    session_id=item.session_id,
                )
                for item in page.items
            ],
            next_cursor=page.next_cursor,
        )


_RESPONSES = {
    **AUTH_RESPONSES,
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required` o `athlete_authorization_required`, sin revelar "
        "si el deportista existe (CA3).",
    },
    HTTPStatus.UNPROCESSABLE_ENTITY.value: {
        "model": ErrorResponse,
        "description": "`validation_error`: periodo, filtro, límite o cursor inválidos (CA6).",
    },
}

_FROM = Query(alias="from", description="Fecha inicial (UTC) de `occurred_at`, inclusiva.")
_TO = Query(alias="to", description="Fecha final (UTC) de `occurred_at`, inclusiva.")


def _invalid(error: InvalidHealthHistoryQuery) -> Response:
    return error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        error.code,
        "La consulta no es válida.",
        [ErrorDetail(field=f"query.{error.field}", issue=error.issue)],
    )


def build_health_history_router(
    *, verify_token: TokenVerifier, get_health_history: GetHealthHistory
) -> APIRouter:
    router = APIRouter(tags=["health-reports"])

    def respond(page: HealthHistoryPage, response: Response) -> HealthReportHistoryResponse:
        response.headers["Cache-Control"] = "no-store"
        return HealthReportHistoryResponse.from_page(page)

    @router.get(
        "/v1/me/health-reports",
        response_model=HealthReportHistoryResponse,
        summary="Mi historial de molestias, incapacidades o lesiones (HU012)",
        responses=_RESPONSES,
    )
    def own_reports(
        *,
        response: Response,
        date_from: Annotated[date | None, _FROM] = None,
        date_to: Annotated[date | None, _TO] = None,
        type: ReportType | None = None,
        severity: Severity | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | HealthReportHistoryResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        filters = HealthHistoryFilters(date_from, date_to, type, severity, limit, cursor)
        try:
            page = get_health_history.own(caller, filters)
        except PermissionRequired as error:
            return forbidden(error)
        except InvalidHealthHistoryQuery as error:
            return _invalid(error)
        return respond(page, response)

    @router.get(
        "/v1/athletes/{athlete_id}/health-reports",
        response_model=HealthReportHistoryResponse,
        summary="Historial de salud de un deportista que me autorizó (HU012)",
        responses=_RESPONSES,
    )
    def athlete_reports(
        *,
        athlete_id: UUID,
        response: Response,
        date_from: Annotated[date | None, _FROM] = None,
        date_to: Annotated[date | None, _TO] = None,
        type: ReportType | None = None,
        severity: Severity | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | HealthReportHistoryResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        filters = HealthHistoryFilters(date_from, date_to, type, severity, limit, cursor)
        try:
            page = get_health_history.of_athlete(caller, athlete_id, filters)
        except (PermissionRequired, AthleteAuthorizationRequired) as error:
            return forbidden(error)
        except InvalidHealthHistoryQuery as error:
            return _invalid(error)
        return respond(page, response)

    return router
