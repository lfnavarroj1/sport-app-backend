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
    """El plan del deportista no incluye la capacidad de la operación (HU017, HU040)."""

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


class ServiceNotBookable(Exception):
    """HU040 CA3: el servicio no existe, no está publicado o no es de entrenamiento virtual
    (`COACHING` con modalidad `VIRTUAL` o `HYBRID`)."""

    code = "service_not_bookable"


class AvailabilityNotBookable(Exception):
    """HU040: el intervalo no es del servicio o empieza en menos de 1 h."""

    code = "availability_not_bookable"


class AvailabilityFull(Exception):
    """HU040 CA2: el intervalo ya tiene tantas citas activas como su capacidad."""

    code = "availability_full"


class AppointmentExists(Exception):
    """HU040 CA2: el deportista ya tiene una cita `BOOKED` en el intervalo."""

    code = "appointment_exists"

    def __init__(self, appointment_id: UUID) -> None:
        super().__init__(str(appointment_id))
        self.appointment_id = appointment_id


class ActiveAppointmentConflict(Exception):
    """Otra solicitud concurrente confirmó primero la cita activa (índice parcial)."""


class AppointmentNotFound(Exception):
    """HU040: la cita no existe o es de otro deportista (no se distingue)."""

    code = "appointment_not_found"


class CancellationWindowClosed(Exception):
    """HU040 CA4: faltan menos de 24 h para el inicio de la cita."""

    code = "cancellation_window_closed"
