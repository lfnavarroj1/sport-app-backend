"""`eventos-read-projector`: proyectar `evento.creado.v1` y `evento.publicado.v1` en
`event_read` y `usuario.inscrito.v1` en `event_registration_read` (CMP-004, ADR-032,
ADR-033).

Eventos (estado completo, ADR-033), en una sola transacción de la base de lectura:
1. si el `event_id` del sobre ya está en `processed_event`, es una entrega repetida;
2. si la vista no existe, se inserta con el estado del mensaje (aunque sea una versión
   posterior a la 1: la reconstrucción no depende de huecos);
3. si la versión recibida es mayor que la proyectada, se sustituyen todas las columnas;
4. si es menor o igual, el mensaje está superado y solo se registra como procesado.
Si otro mensaje del mismo evento crea la vista en paralelo, se reaplica una vez por versión.
El ACK lo envía el worker después de este COMMIT.
"""

import logging
from collections.abc import Callable
from enum import StrEnum

from events_management.application.messages import (
    EventPublishedMessage,
    EventStateMessage,
    RegistrationCreatedMessage,
    SportEventMessage,
)
from events_management.application.ports import (
    Clock,
    ConcurrentEventView,
    DuplicateDelivery,
    ProjectionUnitOfWork,
)
from events_management.domain.registrations.events import EVENT_TYPE as REGISTERED
from events_management.domain.sport_events.created import EVENT_TYPE as CREATED
from events_management.domain.sport_events.published import EVENT_TYPE as PUBLISHED

_logger = logging.getLogger("sportapp.projector")


class ProjectionConflict(Exception):
    """La misma inscripción llegó con otra versión: HU041 no admite actualizaciones y el
    mensaje no se confirma (termina en la DLQ)."""


class ProjectionOutcome(StrEnum):
    PROJECTED = "projected"
    DUPLICATE = "duplicate"
    STALE = "stale"


class ProjectEventState:
    """`evento.creado.v1` y `evento.publicado.v1` por estado completo y versión mayor."""

    def __init__(self, *, unit_of_work: Callable[[], ProjectionUnitOfWork], clock: Clock) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(self, message: EventStateMessage) -> ProjectionOutcome:
        try:
            return self._project(message)
        except DuplicateDelivery:
            return ProjectionOutcome.DUPLICATE
        except ConcurrentEventView:
            # La vista ya existe: la segunda pasada la compara por versión.
            try:
                return self._project(message)
            except DuplicateDelivery:
                return ProjectionOutcome.DUPLICATE

    def _project(self, message: EventStateMessage) -> ProjectionOutcome:
        now = self._clock.now()
        event = message.event
        with self._unit_of_work() as unit_of_work:
            if unit_of_work.is_processed(message.message_id):
                return ProjectionOutcome.DUPLICATE
            projected = unit_of_work.projected_version(event.event_id)
            if projected is None:
                unit_of_work.add_event_view(event, now)
                outcome = ProjectionOutcome.PROJECTED
            elif message.aggregate_version > projected:
                unit_of_work.replace_event_view(event, now)
                outcome = ProjectionOutcome.PROJECTED
            else:
                outcome = ProjectionOutcome.STALE
            event_type = PUBLISHED if isinstance(message, EventPublishedMessage) else CREATED
            unit_of_work.add_processed(message.message_id, event_type, event.event_id, now)
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
        self._events = ProjectEventState(unit_of_work=unit_of_work, clock=clock)
        self._registrations = ProjectRegistrationCreated(unit_of_work=unit_of_work, clock=clock)

    def __call__(self, message: SportEventMessage) -> ProjectionOutcome:
        if isinstance(message, RegistrationCreatedMessage):
            return self._registrations(message)
        return self._events(message)
