"""HU036: umbral vigente del deportista (ADR-044 §9).

1. La configuración propia (`monitoring_settings`) se lee en una transacción corta.
2. Sin configuración, la fecha de nacimiento sale del contexto interno de Usuarios (ADR-041)
   **fuera** de cualquier transacción: `round(0.9 x (220 - edad))` o `171` sin fecha. Si
   Usuarios no responde, `AthleteContextUnavailable`: nunca se evalúa con un umbral supuesto.
"""

from collections.abc import Callable
from uuid import UUID

from training_management.application.ports import (
    AthleteBirthDates,
    Clock,
    MonitoringSettingsUnitOfWork,
)
from training_management.domain.monitoring.model import (
    Threshold,
    ThresholdSource,
    default_threshold,
)


class ResolveThreshold:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], MonitoringSettingsUnitOfWork],
        birth_dates: AthleteBirthDates,
        clock: Clock,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._birth_dates = birth_dates
        self._clock = clock

    def __call__(self, athlete_id: UUID) -> Threshold:
        with self._unit_of_work() as unit_of_work:
            settings = unit_of_work.get_settings(athlete_id)
        if settings is not None:
            return Threshold(settings.max_heart_rate_alert_bpm, ThresholdSource.CUSTOM, settings)
        birth_date = self._birth_dates.birth_date(athlete_id)
        return default_threshold(birth_date, self._clock.now().date())
