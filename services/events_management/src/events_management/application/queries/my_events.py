"""HU024: eventos del organizador autenticado sobre la base de lectura (ADR-033).

- Solo `organizer` (decisión del 2026-10-18: sin permiso adicional, porque HU018 y HU022
  la reutilizarán); otro actor → `permission_required`.
- Solo los eventos propios; `status` opcional (`DRAFT` o `PUBLISHED`; sin él, ambos).
- Orden `starts_at`, `event_id` ascendente con cursor keyset opaco; `limit` de 1 a 100.
- Lee la proyección: un evento recién publicado cambia de estado cuando se proyecta.
- HU025 (ADR-040): cada evento lleva `registration_count`, contado en la base de escritura
  para advertir que cambiar la tarifa no modifica las inscripciones existentes.
"""

from dataclasses import dataclass, field
from uuid import UUID

from events_management.application.errors import PermissionRequired
from events_management.application.ports import (
    Caller,
    EventPosition,
    OwnEventReader,
    OwnEventsQuery,
    RegistrationCounter,
)
from events_management.application.queries.published_events import (
    InvalidEventsQuery,
    decode_cursor,
    encode_cursor,
)
from events_management.domain.sport_events.model import EventStatus, SportEvent

ORGANIZER = "organizer"
MAX_LIMIT = 100
DEFAULT_LIMIT = 20


@dataclass(frozen=True)
class MyEventsFilters:
    status: str | None = None
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None


@dataclass(frozen=True)
class MyEventsPage:
    items: tuple[SportEvent, ...]
    next_cursor: str | None
    registration_counts: dict[UUID, int] = field(default_factory=dict)

    def registration_count(self, event: SportEvent) -> int:
        return self.registration_counts.get(event.event_id, 0)


class ListMyEvents:
    def __init__(self, *, reader: OwnEventReader, counter: RegistrationCounter) -> None:
        self._reader = reader
        self._counter = counter

    def __call__(self, caller: Caller, filters: MyEventsFilters) -> MyEventsPage:
        if caller.actor_type != ORGANIZER:
            raise PermissionRequired()
        if filters.status is not None and filters.status not in {s.value for s in EventStatus}:
            raise InvalidEventsQuery("status", "unknown_value")
        if not 1 <= filters.limit <= MAX_LIMIT:
            raise InvalidEventsQuery("limit", "out_of_range")
        after = decode_cursor(filters.cursor) if filters.cursor else None
        # Se pide uno de más para saber si existe una página siguiente.
        events = self._reader.list(
            OwnEventsQuery(
                organizer_id=caller.user_id,
                status=filters.status,
                after=after,
                limit=filters.limit + 1,
            )
        )
        page = events[: filters.limit]
        next_cursor = (
            encode_cursor(EventPosition(page[-1].details.starts_at, page[-1].event_id))
            if len(events) > filters.limit
            else None
        )
        counts = self._counter.counts([event.event_id for event in page])
        return MyEventsPage(items=tuple(page), next_cursor=next_cursor, registration_counts=counts)
