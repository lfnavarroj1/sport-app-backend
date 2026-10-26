"""HU033: `GET /v1/me/training-dashboard` (ADR-043).

1. Validar el periodo (sin efectos ni llamadas externas).
2. Solo `athlete` con `training.dashboard.view`, verificada ante Afiliación (fuera de cualquier
   transacción; consulta sin escritura).
3. Dos consultas independientes en la base de lectura: sesiones agrupadas y último resultado
   por indicador. Si una falla, esa fuente se informa `UNAVAILABLE` y el tablero sigue con la
   otra; si fallan las dos, `DashboardSourceUnavailable` (→ `503`).

El tablero solo responde para el `athlete_id` del token (CA4): no hay parámetro de deportista.
"""

from uuid import UUID

from training_management.application.access import require_dashboard_capability
from training_management.application.errors import DashboardSourceUnavailable
from training_management.application.ports import Caller, CapabilityChecker, Clock, DashboardReader
from training_management.domain.dashboard.model import (
    LatestMetric,
    SessionBucket,
    TrainingDashboard,
    build_dashboard,
    resolve_period,
)


class GetTrainingDashboard:
    def __init__(
        self, *, reader: DashboardReader, capabilities: CapabilityChecker, clock: Clock
    ) -> None:
        self._reader = reader
        self._capabilities = capabilities
        self._clock = clock

    def __call__(
        self, caller: Caller, raw_from: str | None, raw_to: str | None
    ) -> TrainingDashboard:
        period = resolve_period(raw_from, raw_to, self._clock.now())
        require_dashboard_capability(caller, self._capabilities)
        athlete_id: UUID = caller.user_id
        buckets: list[SessionBucket] = []
        metrics: list[LatestMetric] = []
        sessions_available = metrics_available = True
        try:
            buckets = self._reader.session_buckets(athlete_id, period)
        except DashboardSourceUnavailable:
            sessions_available = False
        try:
            metrics = self._reader.latest_metrics(athlete_id, period)
        except DashboardSourceUnavailable:
            metrics_available = False
        if not sessions_available and not metrics_available:
            raise DashboardSourceUnavailable()
        return build_dashboard(
            period,
            buckets,
            metrics,
            sessions_available=sessions_available,
            metrics_available=metrics_available,
        )
