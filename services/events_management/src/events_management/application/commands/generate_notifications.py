"""HU039: generación asíncrona de notificaciones de eventos masivos (ADR-045 §8-9).

`GenerateEventMatchNotifications` (lo invoca `eventos-notificaciones-worker` por cada
`evento.publicado.v1`):
1. Deportes que coinciden con la disciplina del evento (`discipline-aliases.v1`); sin
   coincidencias no hay destinatarios.
2. Destinatarios por páginas de hasta 1.000 con `POST /v1/users/athletes/search` (deportes,
   ciudad y país del evento), siempre **fuera** de una transacción.
3. Cada página se inserta en lotes de 100 por transacción: preferencias vigentes (CA2),
   `notification` única por `dedupe_key` (CA3) y `notificacion.solicitada.v1` en la outbox
   solo para las insertadas.
Si Usuarios falla, la excepción llega al worker, que no confirma el mensaje: Pub/Sub lo
reintenta y los lotes ya confirmados se deduplican (CA4).

`GenerateEventReminders` (planificador cada 15 min): inscripciones cuyo evento empieza en
`[ahora + 23 h 45 min, ahora + 24 h 15 min]`, bloqueadas con `FOR UPDATE SKIP LOCKED` en
lotes de 100; una corrida repetida o dos réplicas no duplican el recordatorio (CA1, CA3).
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from events_management.application.ports import (
    AthleteDirectory,
    Clock,
    NotificationUnitOfWork,
    OutboxMessage,
)
from events_management.domain.notifications.disciplines import sports_for_discipline
from events_management.domain.notifications.events import (
    AGGREGATE_TYPE,
    AGGREGATE_VERSION,
    EVENT_TYPE,
    SCHEMA_VERSION,
    requested_payload,
)
from events_management.domain.notifications.model import (
    DEFAULT_PREFERENCES,
    Notification,
    NotificationType,
    new_notification,
    reminder_window,
)
from events_management.domain.sport_events.model import SportEvent

SEARCH_PAGE_SIZE = 1000
BATCH_SIZE = 100

_logger = logging.getLogger("sportapp.notifications")


@dataclass
class GenerationResult:
    generated: int = 0
    duplicates: int = 0
    disabled: int = 0

    def add(self, other: "GenerationResult") -> None:
        self.generated += other.generated
        self.duplicates += other.duplicates
        self.disabled += other.disabled


@dataclass(frozen=True)
class _Candidate:
    recipient_id: UUID
    event: SportEvent
    causation_id: str


def _outbox(
    notification: Notification, correlation_id: UUID | None, causation_id: str
) -> OutboxMessage:
    return OutboxMessage(
        event_id=uuid4(),
        event_type=EVENT_TYPE,
        schema_version=SCHEMA_VERSION,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=notification.notification_id,
        aggregate_version=AGGREGATE_VERSION,
        occurred_at=notification.created_at,
        correlation_id=correlation_id,
        causation_id=causation_id,
        payload=requested_payload(notification),
        ordering_key=str(notification.recipient_id),
    )


def _store_batch(
    unit_of_work: NotificationUnitOfWork,
    notification_type: NotificationType,
    candidates: Sequence[_Candidate],
    *,
    clock: Clock,
    correlation_id: UUID | None,
) -> GenerationResult:
    """Inserta un lote dentro de la transacción abierta y deja su outbox; no confirma."""
    result = GenerationResult()
    preferences = unit_of_work.preferences({candidate.recipient_id for candidate in candidates})
    now = clock.now()
    wanted: list[Notification] = []
    causes: dict[UUID, str] = {}
    for candidate in candidates:
        if not preferences.get(candidate.recipient_id, DEFAULT_PREFERENCES).allows(
            notification_type
        ):
            result.disabled += 1
            continue
        notification = new_notification(
            notification_id=uuid4(),
            notification_type=notification_type,
            event=candidate.event,
            recipient_id=candidate.recipient_id,
            now=now,
        )
        wanted.append(notification)
        causes[notification.notification_id] = candidate.causation_id
    inserted = unit_of_work.add_notifications(wanted) if wanted else []
    for notification in inserted:
        unit_of_work.add_outbox(
            _outbox(notification, correlation_id, causes[notification.notification_id])
        )
    result.generated = len(inserted)
    result.duplicates = len(wanted) - len(inserted)
    return result


class GenerateEventMatchNotifications:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], NotificationUnitOfWork],
        directory: AthleteDirectory,
        aliases: dict[str, frozenset[str]],
        clock: Clock,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._directory = directory
        self._aliases = aliases
        self._clock = clock

    def __call__(
        self, event: SportEvent, *, message_id: UUID, correlation_id: UUID | None
    ) -> GenerationResult:
        result = GenerationResult()
        sports = sports_for_discipline(event.details.discipline, self._aliases)
        if not sports:
            return result
        location = event.details.location
        cursor: str | None = None
        while True:
            page = self._directory.search(
                sports=sports,
                city=location.city,
                country_code=location.country_code,
                cursor=cursor,
                limit=SEARCH_PAGE_SIZE,
            )
            for start in range(0, len(page.user_ids), BATCH_SIZE):
                batch = [
                    _Candidate(user_id, event, str(message_id))
                    for user_id in page.user_ids[start : start + BATCH_SIZE]
                ]
                with self._unit_of_work() as unit_of_work:
                    stored = _store_batch(
                        unit_of_work,
                        NotificationType.EVENT_PUBLISHED_MATCH,
                        batch,
                        clock=self._clock,
                        correlation_id=correlation_id,
                    )
                    unit_of_work.commit()
                result.add(stored)
            if page.next_cursor is None:
                return result
            cursor = page.next_cursor


class GenerateEventReminders:
    def __init__(self, *, unit_of_work: Callable[[], NotificationUnitOfWork], clock: Clock) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(self) -> GenerationResult:
        result = GenerationResult()
        starts_from, starts_to = reminder_window(self._clock.now())
        # Una corrida es una causa común de todos sus recordatorios.
        correlation_id = uuid4()
        after: UUID | None = None
        while True:
            with self._unit_of_work() as unit_of_work:
                due = unit_of_work.due_reminders(starts_from, starts_to, after, BATCH_SIZE)
                if not due:
                    return result
                stored = _store_batch(
                    unit_of_work,
                    NotificationType.EVENT_REMINDER_24H,
                    [
                        _Candidate(item.athlete_id, item.event, str(item.registration_id))
                        for item in due
                    ],
                    clock=self._clock,
                    correlation_id=correlation_id,
                )
                unit_of_work.commit()
            result.add(stored)
            if len(due) < BATCH_SIZE:
                return result
            after = due[-1].registration_id
