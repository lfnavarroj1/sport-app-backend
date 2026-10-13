"""Errores de la capa de aplicación, con códigos estables para el contrato de HU021."""


class IdempotencyKeyReused(Exception):
    """La misma clave del organizador se usó con una solicitud diferente."""

    code = "idempotency_key_reused"


class IdempotencyKeyConflict(Exception):
    """Otra solicitud concurrente confirmó primero la misma clave."""


class PermissionRequired(Exception):
    """El actor no es `organizer` o no tiene `events.create` (ADR-025, ADR-028)."""

    code = "permission_required"
