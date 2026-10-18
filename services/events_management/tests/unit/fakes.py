"""Dobles en memoria para probar HU021 sin infraestructura."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from events_management.application.errors import IdempotencyKeyConflict
from events_management.application.ports import (
    ConcurrentEventView,
    DuplicateDelivery,
    IdempotencyRecord,
    OutboxMessage,
)
from events_management.domain.registrations.model import EventRegistration
from events_management.domain.sport_events.model import SportEvent

NOW = datetime(2026, 10, 12, 21, 0, tzinfo=UTC)


class FixedClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


class FakePermissions:
    """`actor-permissions.v1`: solo `organizer` tiene `events.create`."""

    def allows(self, actor_type: str, permission: str) -> bool:
        return actor_type == "organizer" and permission == "events.create"


class NobodyAllowed:
    def allows(self, actor_type: str, permission: str) -> bool:
        return False


@dataclass
class InMemoryStore:
    events: dict[UUID, SportEvent] = field(default_factory=dict)
    outbox: list[OutboxMessage] = field(default_factory=list)
    idempotency: dict[tuple[UUID, str, UUID], IdempotencyRecord] = field(default_factory=dict)


class InMemoryEventCreationUnitOfWork:
    """Aplica los cambios solo al confirmar y valida la misma unicidad que PostgreSQL."""

    def __init__(
        self, store: InMemoryStore, before_commit: Callable[[], None] | None = None
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self._events: list[SportEvent] = []
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
        self._events, self._outbox, self._records, self._deleted = [], [], [], []

    def find_idempotency(
        self, actor_id: UUID, operation: str, key: UUID
    ) -> IdempotencyRecord | None:
        return self.store.idempotency.get((actor_id, operation, key))

    def delete_idempotency(self, actor_id: UUID, operation: str, key: UUID) -> None:
        self._deleted.append((actor_id, operation, key))

    def add_event(self, event: SportEvent) -> None:
        self._events.append(event)

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
        for event in self._events:
            self.store.events[event.event_id] = event
        self.store.outbox.extend(self._outbox)
        for record in self._records:
            self.store.idempotency[(record.actor_id, record.operation, record.key)] = record


@dataclass
class ReadStore:
    views: dict[UUID, SportEvent] = field(default_factory=dict)
    processed: dict[UUID, UUID] = field(default_factory=dict)
    registrations: dict[UUID, EventRegistration] = field(default_factory=dict)


class InMemoryProjectionUnitOfWork:
    def __init__(self, store: ReadStore, before_commit: Callable[[], None] | None = None) -> None:
        self.store = store
        self.before_commit = before_commit
        self._views: list[SportEvent] = []
        self._replaced: list[SportEvent] = []
        self._processed: list[tuple[UUID, UUID]] = []
        self._registrations: list[EventRegistration] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: Any) -> None:
        self._views, self._replaced, self._processed, self._registrations = [], [], [], []

    def is_processed(self, event_id: UUID) -> bool:
        return event_id in self.store.processed

    def projected_version(self, event_id: UUID) -> int | None:
        view = self.store.views.get(event_id)
        return view.version if view is not None else None

    def add_event_view(self, event: SportEvent, projected_at: datetime) -> None:
        self._views.append(event)

    def replace_event_view(self, event: SportEvent, projected_at: datetime) -> None:
        self._replaced.append(event)

    def registration_version(self, registration_id: UUID) -> int | None:
        item = self.store.registrations.get(registration_id)
        return item.version if item is not None else None

    def add_registration_view(
        self, registration: EventRegistration, projected_at: datetime
    ) -> None:
        self._registrations.append(registration)

    def add_processed(
        self, message_id: UUID, event_type: str, aggregate_id: UUID, processed_at: datetime
    ) -> None:
        self._processed.append((message_id, aggregate_id))

    def commit(self) -> None:
        if self.before_commit is not None:
            hook, self.before_commit = self.before_commit, None
            hook()
        if any(message_id in self.store.processed for message_id, _ in self._processed):
            raise DuplicateDelivery()
        # Como la clave primaria de `event_read`: otro mensaje del evento creó la vista.
        if any(view.event_id in self.store.views for view in self._views):
            raise ConcurrentEventView()
        for view in (*self._views, *self._replaced):
            self.store.views[view.event_id] = view
        for item in self._registrations:
            self.store.registrations[item.registration_id] = item
        for message_id, aggregate_id in self._processed:
            self.store.processed[message_id] = aggregate_id


def location(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": "Parque Metropolitano Simón Bolívar",
        "address": "Avenida Calle 63 # 60-50",
        "city": "Bogotá",
        "country_code": "CO",
    }
    values.update(overrides)
    return values


def event_body(**overrides: Any) -> dict[str, Any]:
    """Cuerpo válido de `POST /v1/events` (ejemplo de ADR-028) con datos sintéticos."""
    values: dict[str, Any] = {
        "name": "Carrera 10K Bogotá",
        "description": "Carrera recreativa de diez kilómetros para corredores aficionados.",
        "discipline": "Running",
        "location": location(),
        "starts_at": "2026-11-15T07:00:00-05:00",
        "time_zone": "America/Bogota",
        "duration_minutes": 180,
        "capacity": 5000,
    }
    values.update(overrides)
    return values
