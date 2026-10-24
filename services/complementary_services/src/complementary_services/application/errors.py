"""Errores de la capa de aplicación, con códigos estables para el contrato."""

from uuid import UUID


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


class CapabilitiesUnavailable(Exception):
    """HU020: Afiliación no permitió evaluar la capacidad médica; nunca se concede por
    suposición."""

    code = "upstream_unavailable"


class CapabilitiesTimeout(CapabilitiesUnavailable):
    """Afiliación no respondió a tiempo; el contrato responde igual (`503`, ADR-038)."""


class CapabilityRequired(Exception):
    """HU017: el plan del deportista no incluye `services.sports_medicine.access`."""

    code = "capability_required"


class AssociationExists(Exception):
    """HU017 CA3: ya hay una asociación `ACTIVE` del deportista con el servicio."""

    code = "association_exists"

    def __init__(self, association_id: UUID) -> None:
        super().__init__(str(association_id))
        self.association_id = association_id


class ActiveAssociationConflict(Exception):
    """Otra solicitud concurrente confirmó primero la asociación activa (índice parcial)."""


class AssociationNotFound(Exception):
    """HU017: la asociación no existe o es de otro deportista (no se distingue)."""

    code = "association_not_found"
