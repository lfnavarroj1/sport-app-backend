"""Errores de la capa de aplicación (no son reglas de negocio)."""


class IdempotencyKeyReused(Exception):
    """La misma clave se usó con una solicitud diferente."""

    code = "idempotency_key_reused"


class IdempotencyKeyConflict(Exception):
    """Otra solicitud concurrente confirmó primero la misma clave."""
