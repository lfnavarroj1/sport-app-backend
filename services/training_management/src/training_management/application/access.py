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


def require_plan_author(caller: Caller, permissions: PermissionPolicy) -> None:
    if not permissions.allows(caller.actor_type, PLAN_AUTHOR_PERMISSION):
        raise PermissionRequired()


def require_athlete(caller: Caller) -> None:
    if caller.actor_type != ATHLETE:
        raise PermissionRequired()


def require_plan_capability(caller: Caller, capabilities: CapabilityChecker) -> None:
    """Consulta Afiliación fuera de cualquier transacción local."""
    require_athlete(caller)
    if not capabilities.has_capability(caller.user_id, PLAN_MANAGE_CAPABILITY):
        raise CapabilityRequired()
