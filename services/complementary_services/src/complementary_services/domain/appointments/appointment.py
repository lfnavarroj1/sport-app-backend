"""Cita virtual del deportista con un entrenador (HU040, ADR-046).

- Solo se reserva un servicio `PUBLISHED` de categoría `COACHING` con modalidad `VIRTUAL` o
  `HYBRID`, en uno de sus intervalos que empiece por lo menos 1 h después de ahora.
- El intervalo admite `capacity` citas activas (`booked_count < capacity`) y cada
  deportista tiene como máximo una cita activa por intervalo; la base lo garantiza también
  ante solicitudes concurrentes (bloqueo del intervalo e índice único parcial).
- La cita copia del intervalo `starts_at`, `ends_at` (UTC) y `time_zone`; nace `BOOKED`,
  versión 1, con `meeting_url` siempre `None` en el MVP (sin videollamada).
- El deportista cancela si faltan 24 h o más para el inicio: pasa a
  `CANCELLED_BY_ATHLETE` con `cancelled_at` y sube la versión una vez; el intervalo recupera
  el cupo. No hay reprogramación ni cancelación por el profesional; reservar no cobra.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from complementary_services.domain.catalog.offer import (
    Category,
    Modality,
    ServiceOffer,
    ServiceStatus,
)

BOOKING_LEAD_TIME = timedelta(hours=1)
CANCELLATION_NOTICE = timedelta(hours=24)
BOOKABLE_CATEGORY = Category.COACHING
BOOKABLE_MODALITIES = frozenset({Modality.VIRTUAL, Modality.HYBRID})


class AppointmentStatus(StrEnum):
    BOOKED = "BOOKED"
    CANCELLED_BY_ATHLETE = "CANCELLED_BY_ATHLETE"


@dataclass(frozen=True)
class BookableSlot:
    """Intervalo de disponibilidad con su ocupación, tal como lo ve la reserva."""

    availability_id: UUID
    service_id: UUID
    starts_at: datetime  # UTC
    ends_at: datetime  # UTC
    time_zone: str
    capacity: int
    booked_count: int

    @property
    def has_room(self) -> bool:
        return self.booked_count < self.capacity


def is_bookable_service(offer: ServiceOffer | None) -> bool:
    """Servicio publicado de entrenamiento en modalidad virtual o híbrida."""
    return (
        offer is not None
        and offer.status is ServiceStatus.PUBLISHED
        and offer.category is BOOKABLE_CATEGORY
        and offer.modality in BOOKABLE_MODALITIES
    )


def is_bookable_slot(slot: BookableSlot, now: datetime) -> bool:
    """El intervalo empieza por lo menos 1 h después de `now`."""
    return slot.starts_at >= now + BOOKING_LEAD_TIME


@dataclass(frozen=True)
class Appointment:
    appointment_id: UUID
    athlete_id: UUID
    provider_id: UUID
    service_id: UUID
    availability_id: UUID
    starts_at: datetime
    ends_at: datetime
    time_zone: str
    status: AppointmentStatus
    booked_at: datetime
    version: int
    cancelled_at: datetime | None = None
    meeting_url: str | None = None

    @classmethod
    def book(
        cls,
        *,
        appointment_id: UUID,
        athlete_id: UUID,
        offer: ServiceOffer,
        slot: BookableSlot,
        now: datetime,
    ) -> "Appointment":
        if not is_bookable_service(offer) or slot.service_id != offer.service_id:
            raise ValueError("Solo un servicio de entrenamiento virtual publicado se reserva.")
        if not is_bookable_slot(slot, now) or not slot.has_room:
            raise ValueError("El intervalo no admite la reserva.")
        return cls(
            appointment_id=appointment_id,
            athlete_id=athlete_id,
            provider_id=offer.provider_id,
            service_id=offer.service_id,
            availability_id=slot.availability_id,
            starts_at=slot.starts_at,
            ends_at=slot.ends_at,
            time_zone=slot.time_zone,
            status=AppointmentStatus.BOOKED,
            booked_at=now,
            version=1,
        )

    @property
    def is_booked(self) -> bool:
        return self.status is AppointmentStatus.BOOKED

    def can_cancel(self, now: datetime) -> bool:
        """Faltan 24 h o más para el inicio."""
        return self.starts_at - now >= CANCELLATION_NOTICE

    def cancel(self, now: datetime) -> "Appointment":
        """`BOOKED` → `CANCELLED_BY_ATHLETE`; una ya cancelada se devuelve sin cambios."""
        if not self.is_booked:
            return self
        if not self.can_cancel(now):
            raise ValueError("La ventana de cancelación ya cerró.")
        return replace(
            self,
            status=AppointmentStatus.CANCELLED_BY_ATHLETE,
            cancelled_at=now,
            version=self.version + 1,
        )
