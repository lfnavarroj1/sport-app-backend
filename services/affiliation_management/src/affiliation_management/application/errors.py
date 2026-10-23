"""Errores de aplicación de Afiliación con su código estable."""


class PermissionRequired(Exception):
    """Solo el deportista selecciona o consulta su plan (HU016)."""

    code = "permission_required"


class IdempotencyKeyReused(Exception):
    """La clave ya se usó con otra solicitud del mismo deportista."""

    code = "idempotency_key_reused"


class IdempotencyKeyConflict(Exception):
    """Otra solicitud con la misma clave confirmó primero (se devuelve su resultado)."""
