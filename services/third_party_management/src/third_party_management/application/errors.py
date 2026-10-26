"""Errores de la capa de aplicación con códigos estables del contrato de HU031."""

from enum import StrEnum


class PermissionRequired(Exception):
    """Solo el deportista (`athlete`) opera su integración con Strava."""

    code = "permission_required"


class CapabilityRequired(Exception):
    """El plan vigente no incluye `integrations.strava.connect` (ADR-024)."""

    code = "capability_required"


class CapabilitiesUnavailable(Exception):
    """Afiliación no permitió evaluar las capacidades; nunca se conceden por suposición."""

    code = "upstream_unavailable"


class CapabilitiesTimeout(CapabilitiesUnavailable):
    """Afiliación no respondió a tiempo (`504`)."""

    code = "upstream_timeout"


class StravaNotConfigured(Exception):
    """Faltan `STRAVA_*` o la clave no es válida: la operación responde `503` (R)."""

    code = "upstream_unavailable"


class IntegrationNotLinked(Exception):
    """No hay una vinculación `ACTIVE` con Strava (R)."""

    code = "integration_not_linked"


class SyncInProgress(Exception):
    """Ya existe una corrida `QUEUED`/`RUNNING` del deportista."""

    code = "sync_in_progress"


class IdempotencyKeyConflict(Exception):
    """Otra solicitud concurrente confirmó primero la misma clave."""


class SyncRunConflict(Exception):
    """Otra solicitud concurrente creó primero una corrida pendiente (índice único)."""


class AthleteAlreadyLinked(Exception):
    """La cuenta de Strava ya está vinculada `ACTIVE` a otro deportista (índice único)."""


class ProviderFailureKind(StrEnum):
    """Clasificación neutral de una falla del proveedor externo."""

    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    CIRCUIT_OPEN = "circuit_open"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    REJECTED = "rejected"
    INVALID_RESPONSE = "invalid_response"


class ProviderFailure(Exception):
    """El proveedor no entregó una respuesta útil. Nunca lleva cuerpos ni tokens."""

    def __init__(self, kind: ProviderFailureKind, retry_after_seconds: int | None = None) -> None:
        super().__init__(kind.value)
        self.kind = kind
        self.retry_after_seconds = retry_after_seconds
