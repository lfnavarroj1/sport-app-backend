"""`eventos-read-projector`: proyectar `evento.creado.v1` en `event_read` y
`usuario.inscrito.v1` en `event_registration_read` (CMP-004, ADR-032).

En una sola transacción de la base de lectura:
1. si el `event_id` del sobre ya está en `processed_event`, es una entrega repetida;
2. si la vista ya tiene esa versión o una posterior, el mensaje está superado y solo
   se registra como procesado;
3. si es la siguiente versión (la 1 para un evento nuevo), crea la vista y registra el
   mensaje;
4. si falta una versión intermedia, falla sin efectos: el mensaje no se confirma y
   Pub/Sub lo reintenta hasta que llegue la anterior (o termina en la DLQ).
El ACK lo envía el worker después de este COMMIT.
"""

import logging
from collections.abc import Callable
from enum import StrEnum

from events_management.application.messages import (
    EventCreatedMessage,
    RegistrationCreatedMessage,
    SportEventMessage,
)
from events_management.application.ports import Clock, DuplicateDelivery, ProjectionUnitOfWork
from events_management.domain.registrations.events import EVENT_TYPE as REGISTERED
from events_management.domain.sport_events.created import EVENT_TYPE

_logger = logging.getLogger("sportapp.projector")


class ProjectionConflict(Exception):
    """La misma inscripción llegó con otra versión: HU041 no admite actualizaciones y el
    mensaje no se confirma (termina en la DLQ)."""


class ProjectionGap(Exception):
    """Llegó una versión posterior a la siguiente esperada; se reintenta más tarde."""


class ProjectionOutcome(StrEnum):
    PROJECTED = "projected"
    DUPLICATE = "duplicate"
    STALE = "stale"


class ProjectEventCreated:
    def __init__(self, *, unit_of_work: Callable[[], ProjectionUnitOfWork], clock: Clock) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(self, message: EventCreatedMessage) -> ProjectionOutcome:
        try:
            return self._project(message)
        except DuplicateDelivery:
            return ProjectionOutcome.DUPLICATE

    def _project(self, message: EventCreatedMessage) -> ProjectionOutcome:
        now = self._clock.now()
        event_id = message.event.event_id
        with self._unit_of_work() as unit_of_work:
            if unit_of_work.is_processed(message.message_id):
                return ProjectionOutcome.DUPLICATE
            projected = unit_of_work.projected_version(event_id)
            if projected is not None and projected >= message.aggregate_version:
                outcome = ProjectionOutcome.STALE
            elif (projected or 0) + 1 == message.aggregate_version:
                unit_of_work.add_event_view(message.event, now)
                outcome = ProjectionOutcome.PROJECTED
            else:
                _logger.warning(
                    "projector.version_gap",
                    extra={
                        "fields": {
                            "event_id": str(event_id),
                            "projected_version": projected,
                            "event_version": message.aggregate_version,
                        }
                    },
                )
                raise ProjectionGap()
            unit_of_work.add_processed(message.message_id, EVENT_TYPE, event_id, now)
            unit_of_work.commit()
        return outcome


class ProjectRegistrationCreated:
    def __init__(self, *, unit_of_work: Callable[[], ProjectionUnitOfWork], clock: Clock) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(self, message: RegistrationCreatedMessage) -> ProjectionOutcome:
        try:
            return self._project(message)
        except DuplicateDelivery:
            return ProjectionOutcome.DUPLICATE

    def _project(self, message: RegistrationCreatedMessage) -> ProjectionOutcome:
        now = self._clock.now()
        registration = message.registration
        with self._unit_of_work() as unit_of_work:
            if unit_of_work.is_processed(message.message_id):
                return ProjectionOutcome.DUPLICATE
            projected = unit_of_work.registration_version(registration.registration_id)
            if projected is None:
                unit_of_work.add_registration_view(registration, now)
                outcome = ProjectionOutcome.PROJECTED
            elif projected == message.aggregate_version:
                outcome = ProjectionOutcome.STALE
            else:
                _logger.warning(
                    "projector.version_conflict",
                    extra={
                        "fields": {
                            "registration_id": str(registration.registration_id),
                            "projected_version": projected,
                            "event_version": message.aggregate_version,
                        }
                    },
                )
                raise ProjectionConflict()
            unit_of_work.add_processed(
                message.message_id, REGISTERED, registration.registration_id, now
            )
            unit_of_work.commit()
        return outcome


class EventsProjector:
    """Despacha cada mensaje validado de `sportapp.eventos.v1` a su proyección."""

    def __init__(self, *, unit_of_work: Callable[[], ProjectionUnitOfWork], clock: Clock) -> None:
        self._events = ProjectEventCreated(unit_of_work=unit_of_work, clock=clock)
        self._registrations = ProjectRegistrationCreated(unit_of_work=unit_of_work, clock=clock)

    def __call__(self, message: SportEventMessage) -> ProjectionOutcome:
        if isinstance(message, RegistrationCreatedMessage):
            return self._registrations(message)
        return self._events(message)
