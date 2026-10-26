"""HU033 en el canal móvil: tablero de entrenamientos del deportista (contrato de ADR-043).

El BFF autentica (JWKS), exige `athlete` y la capacidad vigente `training.dashboard.view`
(Afiliación) antes de llamar a Entrenamientos; después reenvía `from`/`to` tal como llegan y
devuelve la respuesta sin alterar. Los agregados, las semanas ISO, la validación del periodo y
la nueva verificación de la capacidad los hace `training_management`. El tablero solo responde
para el deportista del token: no hay parámetro de deportista (CA4).
"""

from collections.abc import Callable
from datetime import date, datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, Field

from bff_mobile.api.training_metrics import (
    ATHLETE,
    BEARER_PREFIX,
    _failure,
    _passthrough,
    _unauthenticated,
)
from bff_mobile.clients.training_management import TrainingManagementClient
from bff_mobile.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.errors import ErrorResponse, error_response
from sportapp_platform.tokens import InvalidAccessToken, JwksUnavailable, VerifiedAccessToken

DASHBOARD_CAPABILITY = "training.dashboard.view"
INSTANT_WITH_OFFSET = "Instante ISO 8601 con desfase (`Z` o `±HH:MM`)."

Sport = Literal["running", "cycling", "swimming", "strength", "other"]
SourceStatus = Literal["OK", "UNAVAILABLE"]


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
    STRAVA: Annotated[
        SourceBreakdown, Field(description="Sesiones importadas de Strava (HU031); hoy en cero.")
    ]


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


_EDGE_STATUSES = {
    HTTPStatus.UNAUTHORIZED,
    HTTPStatus.FORBIDDEN,
    HTTPStatus.SERVICE_UNAVAILABLE,
    HTTPStatus.GATEWAY_TIMEOUT,
}


def build_training_dashboard_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    athlete_snapshot: Callable[[UUID], dict[str, Any]],
    training: TrainingManagementClient,
) -> APIRouter:
    router = APIRouter(tags=["training-dashboard"])

    def authenticate_athlete(authorization: str | None) -> tuple[UUID, str] | Response:
        if not authorization or not authorization.lower().startswith(BEARER_PREFIX):
            return _unauthenticated()
        token = authorization[len(BEARER_PREFIX) :].strip()
        try:
            identity = verify_token(token)
            user_id = UUID(identity.subject)
        except (InvalidAccessToken, ValueError):
            return _unauthenticated()
        except JwksUnavailable:
            return _failure(UpstreamUnavailable())
        if identity.actor_type != ATHLETE:
            return error_response(
                HTTPStatus.FORBIDDEN,
                "permission_required",
                "Su tipo de usuario no puede realizar esta operación.",
            )
        return user_id, token

    def authorized_token(authorization: str | None) -> str | Response:
        """Borde: token, actor `athlete` y capacidad antes de llamar a Entrenamientos."""
        caller = authenticate_athlete(authorization)
        if isinstance(caller, Response):
            return caller
        user_id, token = caller
        try:
            snapshot = athlete_snapshot(user_id)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _failure(error)
        granted = snapshot.get("capabilities") or []
        if snapshot.get("subscription_status") == "ACTIVE" and DASHBOARD_CAPABILITY in granted:
            return token
        return error_response(
            HTTPStatus.FORBIDDEN, "capability_required", "Su plan no incluye esta funcionalidad."
        )

    @router.get(
        "/v1/me/training-dashboard",
        response_model=TrainingDashboardResponse,
        summary="Mi tablero de entrenamientos (HU033)",
        description="Totales, distribución por deporte, por semana ISO y por fuente, y el último "
        "resultado por indicador del periodo `[from, to)` (por defecto los últimos 30 días; "
        "como mucho 366). Sin datos: `has_data: false`, totales en cero y listas vacías.",
        responses={
            HTTPStatus.UNAUTHORIZED.value: {
                "model": ErrorResponse,
                "description": "`invalid_access_token`: falta el token o no es válido.",
            },
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
                "description": "`upstream_unavailable`: capacidad no verificable o servicio caído.",
            },
            HTTPStatus.GATEWAY_TIMEOUT.value: {
                "model": ErrorResponse,
                "description": "`upstream_timeout`: capacidad o servicio sin respuesta.",
            },
        },
    )
    def training_dashboard(
        *,
        raw_from: Annotated[
            str | None, Query(alias="from", description=INSTANT_WITH_OFFSET)
        ] = None,
        raw_to: Annotated[str | None, Query(alias="to", description=INSTANT_WITH_OFFSET)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = authorized_token(authorization)
        if isinstance(token, Response):
            token.headers["Cache-Control"] = "no-store"
            return token
        params = {
            name: value for name, value in (("from", raw_from), ("to", raw_to)) if value is not None
        }
        try:
            upstream = training.training_dashboard(token, params)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            response = _failure(error)
        else:
            response = _passthrough(
                upstream, {HTTPStatus.OK, HTTPStatus.UNPROCESSABLE_ENTITY, *_EDGE_STATUSES}
            )
        response.headers["Cache-Control"] = "no-store"
        return response

    return router
