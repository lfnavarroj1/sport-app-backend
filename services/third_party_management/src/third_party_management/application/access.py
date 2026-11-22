"""Reglas de acceso de HU031 que repite el servicio propietario (ADR-024 y ADR-042).

El BFF móvil ya filtró la solicitud, pero el servicio no confía solo en el borde: las
cuatro operaciones autenticadas exigen `athlete` con la capacidad vigente
`integrations.strava.connect`. Afiliación se consulta fuera de toda transacción local.
"""

from third_party_management.application.errors import (
    CapabilityRequired,
    PermissionRequired,
    StravaNotConfigured,
)
from third_party_management.application.ports import Caller, CapabilityChecker, StravaContext

ATHLETE = "athlete"
STRAVA_CAPABILITY = "integrations.strava.connect"


def require_athlete(caller: Caller) -> None:
    if caller.actor_type != ATHLETE:
        raise PermissionRequired()


def require_strava_capability(caller: Caller, capabilities: CapabilityChecker) -> None:
    require_athlete(caller)
    if not capabilities.has_capability(caller.user_id, STRAVA_CAPABILITY):
        raise CapabilityRequired()


def require_configured(strava: StravaContext | None) -> StravaContext:
    if strava is None:
        raise StravaNotConfigured()
    return strava
