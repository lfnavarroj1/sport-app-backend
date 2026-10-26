"""HU033: `GET /v1/me/training-dashboard?from&to` (ADR-043).

Solo el deportista con `training.dashboard.view`; los agregados salen de la base de lectura.
`from`/`to` son instantes con desfase (`2026-10-01T00:00:00-05:00`); el `+` de un desfase
positivo debe ir codificado (`%2B`); si llega decodificado como espacio, se interpreta como
`+`. Las semanas se calculan en la zona del `from`; las fechas de la respuesta conservan ese
desfase y `measured_at` va en UTC (`Z`): el cliente formatea sin alterar el dato.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, Field

from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from training_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    forbidden,
)
from training_management.application.access import DASHBOARD_CAPABILITY
from training_management.application.errors import (
    CapabilitiesTimeout,
    CapabilitiesUnavailable,
    CapabilityRequired,
    DashboardSourceUnavailable,
    PermissionRequired,
)
from training_management.application.queries.training_dashboard import GetTrainingDashboard
from training_management.domain.dashboard.model import (
    Aggregate,
    InvalidDashboardPeriod,
    LatestMetric,
    TrainingDashboard,
)
from training_management.domain.plans.model import Sport

SourceStatus = Literal["OK", "UNAVAILABLE"]
INSTANT_WITH_OFFSET = "Instante ISO 8601 con desfase (`Z` o `±HH:MM`)."


class DashboardPeriodView(BaseModel):
    from_: Annotated[datetime, Field(alias="from", description=INSTANT_WITH_OFFSET)]
    to: Annotated[datetime, Field(description=INSTANT_WITH_OFFSET)]


class DashboardTotals(BaseModel):
    sessions_count: int
    total_duration_minutes: Annotated[int, Field(description="Minutos, `ROUND_HALF_UP`.")]
    avg_duration_minutes: Annotated[int, Field(description="Minutos por sesión; 0 sin datos.")]
    distance_meters: Annotated[
        int | None,
        Field(description="Metros de las sesiones que la registran; `null` si ninguna la tiene."),
    ]


class SportBreakdown(BaseModel):
    sport: Sport
    sessions_count: int
    total_duration_minutes: int
    distance_meters: int | None


class WeekBreakdown(BaseModel):
    week_start: Annotated[date, Field(description="Lunes de la semana ISO en la zona del `from`.")]
    sessions_count: int
    total_duration_minutes: int


class SourceBreakdown(BaseModel):
    sessions_count: int
    total_duration_minutes: int


class DashboardBySource(BaseModel):
    SPORTAPP: SourceBreakdown
    STRAVA: Annotated[SourceBreakdown, Field(description="Sesiones importadas de Strava (HU031).")]


class DashboardMetric(BaseModel):
    """Estimación de campo de ADR-030; nunca es un diagnóstico (`disclaimer_key`)."""

    indicator: Annotated[str, Field(examples=["FTP_ESTIMATED"])]
    value: Annotated[
        int | float, Field(description="Entero con escala 0; con decimales en otro caso.")
    ]
    unit: Annotated[str, Field(examples=["W"])]
    method_code: str
    method_version: int
    measured_at: Annotated[datetime, Field(description="Instante en UTC con sufijo `Z`.")]
    disclaimer_key: Annotated[str, Field(examples=["estimated_not_diagnostic"])]


class DashboardSource(BaseModel):
    source: Literal["training_sessions", "training_metrics"]
    status: SourceStatus


class TrainingDashboardResponse(BaseModel):
    period: DashboardPeriodView
    has_data: bool
    totals: DashboardTotals
    by_sport: list[SportBreakdown]
    by_week: list[WeekBreakdown]
    by_source: DashboardBySource
    latest_metrics: list[DashboardMetric]
    sources: list[DashboardSource]


def _number(value: Decimal) -> int | float:
    exponent = value.as_tuple().exponent
    return int(value) if isinstance(exponent, int) and exponent >= 0 else float(value)


def _source(aggregate: Aggregate) -> dict[str, int]:
    return {
        "sessions_count": aggregate.sessions_count,
        "total_duration_minutes": aggregate.total_minutes,
    }


def _metric(metric: LatestMetric) -> dict[str, object]:
    return {
        "indicator": metric.indicator,
        "value": _number(metric.value),
        "unit": metric.unit,
        "method_code": metric.method_code,
        "method_version": metric.method_version,
        "measured_at": metric.measured_at.astimezone(UTC),
        "disclaimer_key": metric.disclaimer_key,
    }


def dashboard_document(dashboard: TrainingDashboard) -> TrainingDashboardResponse:
    totals = dashboard.totals
    return TrainingDashboardResponse.model_validate(
        {
            "period": {"from": dashboard.period.start, "to": dashboard.period.end},
            "has_data": dashboard.has_data,
            "totals": {
                "sessions_count": totals.sessions_count,
                "total_duration_minutes": totals.total_minutes,
                "avg_duration_minutes": totals.average_minutes,
                "distance_meters": totals.distance,
            },
            "by_sport": [
                {
                    "sport": sport,
                    "sessions_count": aggregate.sessions_count,
                    "total_duration_minutes": aggregate.total_minutes,
                    "distance_meters": aggregate.distance,
                }
                for sport, aggregate in dashboard.by_sport
            ],
            "by_week": [
                {
                    "week_start": week,
                    "sessions_count": aggregate.sessions_count,
                    "total_duration_minutes": aggregate.total_minutes,
                }
                for week, aggregate in dashboard.by_week
            ],
            "by_source": {
                source.value: _source(aggregate) for source, aggregate in dashboard.by_source
            },
            "latest_metrics": [_metric(metric) for metric in dashboard.latest_metrics],
            "sources": [
                {
                    "source": "training_sessions",
                    "status": "OK" if dashboard.sessions_available else "UNAVAILABLE",
                },
                {
                    "source": "training_metrics",
                    "status": "OK" if dashboard.metrics_available else "UNAVAILABLE",
                },
            ],
        }
    )


def build_training_dashboard_router(
    *, verify_token: TokenVerifier, get_dashboard: GetTrainingDashboard
) -> APIRouter:
    router = APIRouter(tags=["training-dashboard"])

    @router.get(
        "/v1/me/training-dashboard",
        response_model=TrainingDashboardResponse,
        response_model_by_alias=True,
        summary="Mi tablero de entrenamientos (HU033)",
        description="Totales, distribución por deporte, por semana ISO y por fuente, y el último "
        "resultado por indicador del periodo `[from, to)` (por defecto los últimos 30 días; "
        "como mucho 366). Sin datos: `has_data: false`, totales en cero y listas vacías.",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` (no es deportista) o `capability_required` "
                f"(`{DASHBOARD_CAPABILITY}`).",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `query.from`/`query.to` con `required` (solo "
                "llegó uno), `invalid_datetime` (sin desfase o mal formado), "
                "`ends_before_starts` (`to` ≤ `from`) u `out_of_range` (más de 366 días).",
            },
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": "`upstream_unavailable`: Afiliación o la base de lectura no "
                "responden.",
            },
            HTTPStatus.GATEWAY_TIMEOUT.value: {
                "model": ErrorResponse,
                "description": "`upstream_timeout`: Afiliación no respondió a tiempo.",
            },
        },
    )
    def training_dashboard(
        *,
        response: Response,
        raw_from: Annotated[
            str | None, Query(alias="from", max_length=40, description=INSTANT_WITH_OFFSET)
        ] = None,
        raw_to: Annotated[
            str | None, Query(alias="to", max_length=40, description=INSTANT_WITH_OFFSET)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | TrainingDashboardResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            dashboard = get_dashboard(caller, raw_from, raw_to)
        except InvalidDashboardPeriod as error:
            return error_response(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                error.code,
                "La consulta no es válida.",
                [ErrorDetail(field=f"query.{error.field}", issue=error.issue)],
            )
        except (PermissionRequired, CapabilityRequired) as error:
            return forbidden(error)
        except CapabilitiesTimeout as error:
            return error_response(
                HTTPStatus.GATEWAY_TIMEOUT,
                error.code,
                "No fue posible verificar su plan a tiempo. Intente de nuevo.",
            )
        except (CapabilitiesUnavailable, DashboardSourceUnavailable) as error:
            return error_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                error.code,
                "No fue posible consultar el tablero. Intente de nuevo.",
            )
        response.headers["Cache-Control"] = "no-store"
        return dashboard_document(dashboard)

    return router
