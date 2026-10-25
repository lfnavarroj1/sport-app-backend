"""HU036: `PUT /v1/me/monitoring-settings` (ADR-044 §9).

1. Solo `athlete`; validar `{max_heart_rate_alert_bpm: 100-220}`.
2. Verificar `training.realtime.monitor` en Afiliación **fuera** de la transacción.
3. En una transacción con la configuración bloqueada: crearla (versión 1) o reemplazar el
   umbral (versión siguiente). Repetir el mismo umbral no cambia nada (PUT idempotente, sin
   `Idempotency-Key`). Si otra solicitud concurrente la creó primero, se reintenta una vez
   sobre la fila ya existente.

Sin evento: el umbral no interesa a otros dominios. El valor no se registra en los logs.
"""

import logging
from collections.abc import Callable

from training_management.application.access import (
    require_athlete,
    require_realtime_monitor_capability,
)
from training_management.application.errors import (
    CapabilitiesUnavailable,
    CapabilityRequired,
    MonitoringSettingsConflict,
    PermissionRequired,
)
from training_management.application.ports import (
    Caller,
    CapabilityChecker,
    Clock,
    MonitoringSettingsUnitOfWork,
)
from training_management.domain.monitoring.model import (
    InvalidMonitoringSettings,
    MonitoringSettings,
    Threshold,
    ThresholdSource,
    validate_settings,
)

_audit = logging.getLogger("sportapp.audit")

_REJECTIONS = (
    PermissionRequired,
    CapabilityRequired,
    CapabilitiesUnavailable,
    InvalidMonitoringSettings,
)


class UpdateMonitoringSettings:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], MonitoringSettingsUnitOfWork],
        capabilities: CapabilityChecker,
        clock: Clock,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._capabilities = capabilities
        self._clock = clock

    def __call__(self, caller: Caller, body: object) -> Threshold:
        try:
            settings = self._execute(caller, body)
        except _REJECTIONS as error:
            self._audit(caller, {"result": "rejected", "reason": getattr(error, "code", "")})
            raise
        self._audit(caller, {"result": "updated", "settings_version": settings.version})
        return Threshold(settings.max_heart_rate_alert_bpm, ThresholdSource.CUSTOM, settings)

    @staticmethod
    def _audit(caller: Caller, fields: dict[str, object]) -> None:
        _audit.info(
            "audit.monitoring_settings_updated",
            extra={"fields": {"athlete_id": str(caller.user_id), **fields}},
        )

    def _execute(self, caller: Caller, body: object) -> MonitoringSettings:
        require_athlete(caller)
        threshold = validate_settings(body)
        require_realtime_monitor_capability(caller, self._capabilities)
        try:
            return self._save(caller, threshold)
        except MonitoringSettingsConflict:
            return self._save(caller, threshold)

    def _save(self, caller: Caller, threshold: int) -> MonitoringSettings:
        now = self._clock.now()
        with self._unit_of_work() as unit_of_work:
            current = unit_of_work.get_settings(caller.user_id, for_update=True)
            if current is None:
                settings = MonitoringSettings.create(caller.user_id, threshold, now)
                unit_of_work.add_settings(settings)
            else:
                settings = current.change(threshold, now)
                if settings is current:
                    return current
                unit_of_work.update_settings(settings)
            unit_of_work.commit()
        return settings
