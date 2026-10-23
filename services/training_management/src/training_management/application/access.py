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
NUTRITION_PLAN_AUTHOR_PERMISSION = "nutrition.plan.author"


def require_plan_author(caller: Caller, permissions: PermissionPolicy) -> None:
    if not permissions.allows(caller.actor_type, PLAN_AUTHOR_PERMISSION):
        raise PermissionRequired()


def require_nutrition_plan_author(caller: Caller, permissions: PermissionPolicy) -> None:
    """HU014 (ADR-037): crear o consultar como autor un plan de alimentación exige el permiso
    por actor `nutrition.plan.author`."""
    if not permissions.allows(caller.actor_type, NUTRITION_PLAN_AUTHOR_PERMISSION):
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
