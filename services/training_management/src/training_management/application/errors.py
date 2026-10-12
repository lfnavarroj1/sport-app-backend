"""Errores de la capa de aplicación, con códigos estables para el contrato de HU010."""


class IdempotencyKeyReused(Exception):
    """La misma clave se usó con una solicitud diferente."""

    code = "idempotency_key_reused"


class IdempotencyKeyConflict(Exception):
    """Otra solicitud concurrente confirmó primero la misma clave."""


class PermissionRequired(Exception):
    """El tipo de actor no tiene el permiso de la operación (ADR-025)."""

    code = "permission_required"


class CapabilityRequired(Exception):
    """El deportista no tiene la capacidad vigente de su plan (ADR-024)."""

    code = "capability_required"


class AthleteAuthorizationRequired(Exception):
    """CA2: no existe una autorización activa del deportista para este profesional."""

    code = "athlete_authorization_required"


class CapabilitiesUnavailable(Exception):
    """Afiliación no permitió evaluar las capacidades; nunca se conceden por suposición."""

    code = "upstream_unavailable"


class TrainingPlanNotFound(Exception):
    """No existe, no es visible para quien consulta o aún no está proyectado."""

    code = "training_plan_not_found"


class AuthorizationConflict(Exception):
    """Otra solicitud concurrente creó primero la misma autorización."""
