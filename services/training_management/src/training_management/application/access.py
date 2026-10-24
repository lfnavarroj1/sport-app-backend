"""Reglas de acceso de HU010 que repite el servicio propietario (ADR-024 y ADR-025).

El BFF ya filtró la solicitud, pero el servicio no confía solo en el borde:
- el profesional necesita el permiso por actor `training.plan.author`;
- el deportista necesita la capacidad vigente `training.plan.manage` de su plan.
"""

from training_management.application.errors import (
    CapabilityRequired,
    PermissionRequired,
)
from training_management.application.ports import Caller, CapabilityChecker, PermissionPolicy

ATHLETE = "athlete"
PLAN_AUTHOR_PERMISSION = "training.plan.author"
PLAN_MANAGE_CAPABILITY = "training.plan.manage"
METRICS_CAPABILITY = "training.metrics.ftp_vo2max"
DASHBOARD_CAPABILITY = "training.dashboard.view"
NUTRITION_PLAN_AUTHOR_PERMISSION = "nutrition.plan.author"
ROUTE_AUTHOR_PERMISSION = "training.routes.author"
ROUTE_SUGGEST_CAPABILITY = "training.routes.suggest"
TEMPLATE_SUGGEST_CAPABILITY = "training.plans.suggest"
DEVICES_CAPABILITY = "devices.measurement.connect"
REALTIME_MONITOR_CAPABILITY = "training.realtime.monitor"


def require_plan_author(caller: Caller, permissions: PermissionPolicy) -> None:
    if not permissions.allows(caller.actor_type, PLAN_AUTHOR_PERMISSION):
        raise PermissionRequired()


def require_nutrition_plan_author(caller: Caller, permissions: PermissionPolicy) -> None:
    """HU014 (ADR-037): crear o consultar como autor un plan de alimentación exige el permiso
    por actor `nutrition.plan.author`."""
    if not permissions.allows(caller.actor_type, NUTRITION_PLAN_AUTHOR_PERMISSION):
        raise PermissionRequired()


def require_route_author(caller: Caller, permissions: PermissionPolicy) -> None:
    """HU026 (ADR-041): publicar, retirar y listar rutas propias exige el permiso por actor
    `training.routes.author` (`actor-permissions.v2`, solo `organizer` en web)."""
    if not permissions.allows(caller.actor_type, ROUTE_AUTHOR_PERMISSION):
        raise PermissionRequired()


def require_athlete(caller: Caller) -> None:
    if caller.actor_type != ATHLETE:
        raise PermissionRequired()


def require_plan_capability(caller: Caller, capabilities: CapabilityChecker) -> None:
    """Consulta Afiliación fuera de cualquier transacción local."""
    require_athlete(caller)
    if not capabilities.has_capability(caller.user_id, PLAN_MANAGE_CAPABILITY):
        raise CapabilityRequired()


def require_metrics_capability(caller: Caller, capabilities: CapabilityChecker) -> None:
    """HU034 (ADR-030): catálogo, cálculo e historial exigen `athlete` con
    `training.metrics.ftp_vo2max`. Consulta Afiliación fuera de cualquier transacción."""
    require_athlete(caller)
    if not capabilities.has_capability(caller.user_id, METRICS_CAPABILITY):
        raise CapabilityRequired()


def require_dashboard_capability(caller: Caller, capabilities: CapabilityChecker) -> None:
    """HU033 (ADR-043): el tablero exige `athlete` con `training.dashboard.view`. Consulta
    Afiliación antes de leer."""
    require_athlete(caller)
    if not capabilities.has_capability(caller.user_id, DASHBOARD_CAPABILITY):
        raise CapabilityRequired()


def require_route_suggest_capability(caller: Caller, capabilities: CapabilityChecker) -> None:
    """HU026 (ADR-041): las sugerencias de rutas exigen `athlete` con
    `training.routes.suggest`. Consulta Afiliación antes de leer."""
    require_athlete(caller)
    if not capabilities.has_capability(caller.user_id, ROUTE_SUGGEST_CAPABILITY):
        raise CapabilityRequired()


def require_template_suggest_capability(caller: Caller, capabilities: CapabilityChecker) -> None:
    """HU028 (ADR-041): las sugerencias de plantillas exigen `athlete` con
    `training.plans.suggest`. Consulta Afiliación antes de leer."""
    require_athlete(caller)
    if not capabilities.has_capability(caller.user_id, TEMPLATE_SUGGEST_CAPABILITY):
        raise CapabilityRequired()


def require_devices_capability(caller: Caller, capabilities: CapabilityChecker) -> None:
    """HU037 (ADR-044): vincular, listar y revocar dispositivos exige `athlete` con
    `devices.measurement.connect`. Consulta Afiliación fuera de cualquier transacción."""
    require_athlete(caller)
    if not capabilities.has_capability(caller.user_id, DEVICES_CAPABILITY):
        raise CapabilityRequired()


def require_realtime_monitor_capability(caller: Caller, capabilities: CapabilityChecker) -> None:
    """HU037/HU036 (ADR-044 §5): la ingestión de mediciones exige `athlete` con
    `training.realtime.monitor`. Consulta Afiliación fuera de cualquier transacción."""
    require_athlete(caller)
    if not capabilities.has_capability(caller.user_id, REALTIME_MONITOR_CAPABILITY):
        raise CapabilityRequired()
