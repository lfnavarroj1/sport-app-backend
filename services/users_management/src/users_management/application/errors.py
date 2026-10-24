"""Errores de la capa de aplicación (no son reglas de negocio)."""


class IdempotencyKeyReused(Exception):
    """La misma clave se usó con una solicitud diferente."""

    code = "idempotency_key_reused"


class IdempotencyKeyConflict(Exception):
    """Otra solicitud concurrente confirmó primero la misma clave."""


class SectionAlreadyExists(Exception):
    """Otra solicitud concurrente creó primero la misma sección del perfil (HU005)."""


class ReadModelUnavailable(Exception):
    """La base de lectura no respondió; la consulta se degrada a `503`."""

    code = "upstream_unavailable"


class AccountNotFound(Exception):
    """El `sub` del token no corresponde a ninguna cuenta (HU002); se responde `401`."""
