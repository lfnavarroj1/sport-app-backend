"""HU036: `GET /v1/me/monitoring-settings` (ADR-044 §9).

Solo `athlete` con `training.realtime.monitor` (Afiliación antes de leer). Devuelve el umbral
vigente y su origen: el configurado (`CUSTOM`), el calculado con la edad (`AGE_DEFAULT`) o
`171` sin fecha de nacimiento (`FIXED_DEFAULT`).
"""

from training_management.application.access import require_realtime_monitor_capability
from training_management.application.monitoring_threshold import ResolveThreshold
from training_management.application.ports import Caller, CapabilityChecker
from training_management.domain.monitoring.model import Threshold


class GetMonitoringSettings:
    def __init__(self, *, resolve: ResolveThreshold, capabilities: CapabilityChecker) -> None:
        self._resolve = resolve
        self._capabilities = capabilities

    def __call__(self, caller: Caller) -> Threshold:
        require_realtime_monitor_capability(caller, self._capabilities)
        return self._resolve(caller.user_id)
