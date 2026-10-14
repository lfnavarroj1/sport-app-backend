"""Errores de la capa de aplicación, con códigos estables para el contrato."""


class PermissionRequired(Exception):
    """El tipo de actor no tiene el permiso de la operación (ADR-025)."""

    code = "permission_required"


class ServiceNotFound(Exception):
    """El servicio no existe o pertenece a otro profesional (no se distingue, HU019 CA3)."""

    code = "service_not_found"


class IdempotencyKeyReused(Exception):
    """La misma clave del profesional se usó con otra solicitud."""

    code = "idempotency_key_reused"


class IdempotencyKeyConflict(Exception):
    """Otra solicitud concurrente del mismo profesional confirmó primero la misma clave."""
