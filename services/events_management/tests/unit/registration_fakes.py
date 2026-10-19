"""Dobles en memoria para probar HU041 sin infraestructura."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

from events_management.application.errors import IdempotencyKeyConflict, RegistrationConflict
from events_management.application.ports import (
    CalendarEventView,
    EventDetailView,
    EventSummary,
    IdempotencyRecord,
    OutboxMessage,
    RegistrationsQuery,
    RegistrationView,
)
from events_management.domain.registrations.model import EventForRegistration, EventRegistration
from events_management.domain.sport_events.model import EventStatus

NOW = datetime(2026, 10, 12, 21, 0, tzinfo=UTC)


def summary(starts_at: datetime) -> EventSummary:
    return EventSummary(
        name="Carrera 10K Bogotá",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=3),
        time_zone="America/Bogota",
        location_name="Parque Metropolitano Simón Bolívar",
        location_city="Bogotá",
        location_country_code="CO",
    )


@dataclass
class StoredEvent:
    event: EventForRegistration
    summary: EventSummary


@dataclass
class InMemoryRegistrationStore:
    events: dict[UUID, StoredEvent] = field(default_factory=dict)
    registrations: dict[UUID, EventRegistration] = field(default_factory=dict)
    outbox: list[OutboxMessage] = field(default_factory=list)
    idempotency: dict[tuple[UUID, str, UUID], IdempotencyRecord] = field(default_factory=dict)

    def publish(
        self,
        *,
        capacity: int = 10,
        starts_at: datetime | None = None,
        status: EventStatus = EventStatus.PUBLISHED,
    ) -> UUID:
        event_id = uuid4()
        starts = starts_at or NOW + timedelta(days=30)
        self.events[event_id] = StoredEvent(
            EventForRegistration(
                event_id=event_id, status=status, starts_at=starts, capacity=capacity
            ),
            summary(starts),
        )
        return event_id


class InMemoryRegistrationUnitOfWork:
    """Aplica los cambios solo al confirmar y valida las mismas unicidades que PostgreSQL."""

    def __init__(
        self, store: InMemoryRegistrationStore, before_commit: Callable[[], None] | None = None
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self._registrations: list[EventRegistration] = []
        self._outbox: list[OutboxMessage] = []
        self._records: list[IdempotencyRecord] = []
        self._deleted: list[tuple[UUID, str, UUID]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._registrations, self._outbox, self._records, self._deleted = [], [], [], []

    def find_idempotency(
        self, actor_id: UUID, operation: str, key: UUID
    ) -> IdempotencyRecord | None:
        return self.store.idempotency.get((actor_id, operation, key))

    def delete_idempotency(self, actor_id: UUID, operation: str, key: UUID) -> None:
        self._deleted.append((actor_id, operation, key))

    def lock_event(self, event_id: UUID) -> EventForRegistration | None:
        stored = self.store.events.get(event_id)
        return stored.event if stored else None

    def event_summary(self, event_id: UUID) -> EventSummary:
        return self.store.events[event_id].summary

    def count_registrations(self, event_id: UUID) -> int:
        return sum(r.event_id == event_id for r in self.store.registrations.values())

    def find_registration(self, event_id: UUID, athlete_id: UUID) -> EventRegistration | None:
        return next(
            (
                r
                for r in self.store.registrations.values()
                if r.event_id == event_id and r.athlete_id == athlete_id
            ),
            None,
        )

    def add_registration(self, registration: EventRegistration) -> None:
        self._registrations.append(registration)

    def add_outbox(self, message: OutboxMessage) -> None:
        self._outbox.append(message)

    def add_idempotency(self, record: IdempotencyRecord) -> None:
        self._records.append(record)

    def commit(self) -> None:
        if self.before_commit is not None:
            hook, self.before_commit = self.before_commit, None
            hook()
        for key in self._deleted:
            self.store.idempotency.pop(key, None)
        if any((r.actor_id, r.operation, r.key) in self.store.idempotency for r in self._records):
            raise IdempotencyKeyConflict()
        for registration in self._registrations:
            if self.find_registration(registration.event_id, registration.athlete_id):
                raise RegistrationConflict()
        for registration in self._registrations:
            self.store.registrations[registration.registration_id] = registration
        self.store.outbox.extend(self._outbox)
        for record in self._records:
            self.store.idempotency[(record.actor_id, record.operation, record.key)] = record


class InMemoryEventDetailReader:
    def __init__(
        self, events: dict[UUID, EventDetailView], owners: dict[UUID, UUID] | None = None
    ) -> None:
        self.events = events
        # Organizador de cada evento: solo él ve su borrador (ADR-033).
        self.owners = owners if owners is not None else {}

    def visible_detail(
        self, event_id: UUID, *, athlete_id: UUID | None, owner_id: UUID | None
    ) -> EventDetailView | None:
        detail = self.events.get(event_id)
        if detail is None:
            return None
        if detail.event.status != "PUBLISHED" and self.owners.get(event_id) != owner_id:
            return None
        mine = detail.my_registration
        if mine is not None and mine.athlete_id != athlete_id:
            mine = None
        return EventDetailView(detail.event, detail.description, detail.capacity, mine)


class InMemoryRegistrationReader:
    def __init__(self, views: list[RegistrationView]) -> None:
        self.views = views

    def list(self, query: RegistrationsQuery) -> list[RegistrationView]:
        selected = sorted(
            (
                v
                for v in self.views
                if v.registration.athlete_id == query.athlete_id
                and (query.status is None or v.registration.status.value == query.status)
                and (
                    query.after is None
                    or (v.event.starts_at, v.registration.registration_id)
                    > (query.after.starts_at, query.after.registration_id)
                )
            ),
            key=lambda v: (v.event.starts_at, v.registration.registration_id),
        )
        return selected[: query.limit]


def calendar_view(
    event_id: UUID, starts_at: datetime, status: str = "PUBLISHED"
) -> CalendarEventView:
    item = summary(starts_at)
    return CalendarEventView(
        event_id=event_id,
        name=item.name,
        discipline="Running",
        location_name=item.location_name,
        location_city=item.location_city,
        location_country_code=item.location_country_code,
        starts_at=starts_at,
        ends_at=item.ends_at,
        time_zone=item.time_zone,
        status=status,
        version=2,
    )
