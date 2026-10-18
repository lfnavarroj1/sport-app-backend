"""Dobles en memoria para probar HU024 (publicación y eventos propios) sin infraestructura."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fakes import NOW, InMemoryStore, event_body

from events_management.api.payload import parse_event
from events_management.application.errors import IdempotencyKeyConflict
from events_management.application.ports import (
    IdempotencyRecord,
    OutboxMessage,
    OwnEventsQuery,
)
from events_management.domain.sport_events.model import SportEvent, validate_event


class PublishPermissions:
    """`actor-permissions.v1`: solo `organizer` tiene `events.publish` (y `events.create`)."""

    def allows(self, actor_type: str, permission: str) -> bool:
        return actor_type == "organizer" and permission in {"events.create", "events.publish"}


class InMemoryPublicationUnitOfWork:
    """Aplica los cambios solo al confirmar y valida la misma unicidad que PostgreSQL."""

    def __init__(
        self, store: InMemoryStore, before_commit: Callable[[], None] | None = None
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self.locked: list[UUID] = []
        self._updated: list[SportEvent] = []
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
        self._updated, self._outbox, self._records, self._deleted = [], [], [], []

    def find_idempotency(
        self, actor_id: UUID, operation: str, key: UUID
    ) -> IdempotencyRecord | None:
        return self.store.idempotency.get((actor_id, operation, key))

    def delete_idempotency(self, actor_id: UUID, operation: str, key: UUID) -> None:
        self._deleted.append((actor_id, operation, key))

    def lock_event(self, event_id: UUID) -> SportEvent | None:
        self.locked.append(event_id)
        return self.store.events.get(event_id)

    def update_event(self, event: SportEvent) -> None:
        self._updated.append(event)

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
        for event in self._updated:
            self.store.events[event.event_id] = event
        self.store.outbox.extend(self._outbox)
        for record in self._records:
            self.store.idempotency[(record.actor_id, record.operation, record.key)] = record


class InMemoryOwnEventReader:
    """Mismo filtro, orden y keyset que `SqlAlchemyOwnEventReader`."""

    def __init__(self, events: list[SportEvent]) -> None:
        self.events = events
        self.queries: list[OwnEventsQuery] = []

    def list(self, query: OwnEventsQuery) -> list[SportEvent]:
        self.queries.append(query)
        selected = sorted(
            (
                event
                for event in self.events
                if event.organizer_id == query.organizer_id
                and (query.status is None or event.status.value == query.status)
            ),
            key=lambda event: (event.details.starts_at, event.event_id),
        )
        if query.after is not None:
            after = (query.after.starts_at, query.after.event_id)
            selected = [e for e in selected if (e.details.starts_at, e.event_id) > after]
        return selected[: query.limit]


def draft(
    organizer_id: UUID,
    *,
    starts_in: timedelta | None = None,
    now: datetime = NOW,
    **overrides: Any,
) -> SportEvent:
    """Borrador válido de ADR-028 persistido (versión 1) para el organizador."""
    body = event_body(**overrides)
    if starts_in is not None:
        # Con el desfase de la zona declarada, como lo enviaría el organizador.
        body["starts_at"] = (now + starts_in).astimezone(ZoneInfo(body["time_zone"])).isoformat()
    return SportEvent.draft(
        event_id=uuid4(),
        organizer_id=organizer_id,
        details=validate_event(parse_event(body), now),
        now=now,
    )


def with_details(event: SportEvent, **changes: Any) -> SportEvent:
    """Agregado persistido que ya no cumple ADR-028 (p. ej., datos anteriores a una regla)."""
    return replace(event, details=replace(event.details, **changes))
