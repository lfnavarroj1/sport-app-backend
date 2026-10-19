"""Inscripción del deportista a un evento publicado (HU041, ADR-032).

- Solo eventos `PUBLISHED` cuyo `starts_at` es posterior al instante del comando; uno
  inexistente o en borrador «no existe» para el deportista.
- Una inscripción por deportista y evento; el cupo (`capacity`) se decide en la escritura
  con el evento bloqueado, nunca con la proyección.
- Nace `REGISTERED`, versión 1. `applied_fee` es informativo (tarifa de HU025 si existe);
  no hay cobro, factura ni estado de pago.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from events_management.domain.sport_events.model import EventStatus

INITIAL_VERSION = 1


class RegistrationStatus(StrEnum):
    REGISTERED = "REGISTERED"


class EventNotAvailable(Exception):
    """El evento no existe o no está publicado: se responde como inexistente."""

    code = "event_not_found"


class EventRegistrationClosed(Exception):
    """El evento publicado ya inició."""

    code = "event_registration_closed"


class EventFull(Exception):
    """El evento alcanzó su cupo."""

    code = "event_full"


@dataclass(frozen=True)
class AppliedFee:
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class EventForRegistration:
    """Lo que la escritura necesita del evento bloqueado para decidir la inscripción."""

    event_id: UUID
    status: EventStatus
    starts_at: datetime
    capacity: int
    fee: AppliedFee | None = None


@dataclass(frozen=True)
class EventRegistration:
    registration_id: UUID
    event_id: UUID
    athlete_id: UUID
    status: RegistrationStatus
    registered_at: datetime
    applied_fee: AppliedFee | None
    version: int
    created_at: datetime
    updated_at: datetime


def register(
    *,
    registration_id: UUID,
    athlete_id: UUID,
    event: EventForRegistration | None,
    registered_count: int,
    now: datetime,
) -> EventRegistration:
    """Inscripción nueva o la regla incumplida (inexistente, cerrado o sin cupo)."""
    if event is None or event.status is not EventStatus.PUBLISHED:
        raise EventNotAvailable()
    if event.starts_at <= now:
        raise EventRegistrationClosed()
    if registered_count >= event.capacity:
        raise EventFull()
    instant = now.astimezone(UTC)
    return EventRegistration(
        registration_id=registration_id,
        event_id=event.event_id,
        athlete_id=athlete_id,
        status=RegistrationStatus.REGISTERED,
        registered_at=instant,
        applied_fee=event.fee,
        version=INITIAL_VERSION,
        created_at=instant,
        updated_at=instant,
    )
