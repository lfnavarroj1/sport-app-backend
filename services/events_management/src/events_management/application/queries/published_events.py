"""HU035: eventos publicados por periodo para el calendario del deportista (ADR-031).

- Solo `athlete`, con la capacidad vigente `calendar.sport.view` consultada en Afiliación
  (el acceso del organizador pertenece a HU023 y reutilizará esta consulta).
- Eventos `PUBLISHED` cuyo intervalo `[starts_at, ends_at]` se superpone con `[from, to]`;
  nunca borradores. Orden `starts_at`, `event_id` con cursor keyset opaco.
- Lee la base de lectura; un evento recién publicado aparece cuando se proyecta.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from events_management.application.errors import CapabilityRequired, PermissionRequired
from events_management.application.ports import (
    CalendarEventView,
    Caller,
    CapabilityChecker,
    EventPosition,
    PublishedEventReader,
    PublishedEventsQuery,
)

ATHLETE = "athlete"
CALENDAR_CAPABILITY = "calendar.sport.view"
MAX_LIMIT = 200
DEFAULT_LIMIT = 200


class InvalidEventsQuery(Exception):
    """Periodo, límite o cursor inválido; se informa el parámetro afectado."""

    code = "validation_error"

    def __init__(self, field: str, issue: str) -> None:
        super().__init__(f"{field}: {issue}")
        self.field = field
        self.issue = issue


@dataclass(frozen=True)
class EventsFilters:
    period_from: datetime
    period_to: datetime
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None


@dataclass(frozen=True)
class EventsPage:
    items: tuple[CalendarEventView, ...]
    next_cursor: str | None


def encode_cursor(position: EventPosition) -> str:
    raw = json.dumps(
        {"t": position.starts_at.isoformat(), "e": str(position.event_id)}, separators=(",", ":")
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> EventPosition:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        starts_at = datetime.fromisoformat(document["t"])
        event_id = UUID(document["e"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        raise InvalidEventsQuery("cursor", "invalid_cursor") from None
    if starts_at.tzinfo is None:
        raise InvalidEventsQuery("cursor", "invalid_cursor")
    return EventPosition(starts_at=starts_at, event_id=event_id)


class ListPublishedEvents:
    def __init__(self, *, reader: PublishedEventReader, capabilities: CapabilityChecker) -> None:
        self._reader = reader
        self._capabilities = capabilities

    def validate(self, filters: EventsFilters) -> EventPosition | None:
        for field, value in (("from", filters.period_from), ("to", filters.period_to)):
            if value.tzinfo is None:
                raise InvalidEventsQuery(field, "invalid_datetime")
        if filters.period_to < filters.period_from:
            raise InvalidEventsQuery("to", "ends_before_starts")
        if not 1 <= filters.limit <= MAX_LIMIT:
            raise InvalidEventsQuery("limit", "out_of_range")
        return decode_cursor(filters.cursor) if filters.cursor else None

    def __call__(self, caller: Caller, filters: EventsFilters) -> EventsPage:
        if caller.actor_type != ATHLETE:
            raise PermissionRequired()
        after = self.validate(filters)
        # Afiliación se consulta fuera de cualquier transacción; nunca se concede por suposición.
        if not self._capabilities.has_capability(caller.user_id, CALENDAR_CAPABILITY):
            raise CapabilityRequired()
        # Se pide uno de más para saber si existe una página siguiente.
        events = self._reader.published(
            PublishedEventsQuery(
                starts_before_or_at=filters.period_to,
                ends_at_or_after=filters.period_from,
                after=after,
                limit=filters.limit + 1,
            )
        )
        page = events[: filters.limit]
        next_cursor = (
            encode_cursor(EventPosition(page[-1].starts_at, page[-1].event_id))
            if len(events) > filters.limit
            else None
        )
        return EventsPage(items=tuple(page), next_cursor=next_cursor)
