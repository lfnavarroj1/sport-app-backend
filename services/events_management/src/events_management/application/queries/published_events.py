"""Eventos publicados por periodo (ADR-031 para HU035, ampliado por ADR-040 para HU023).

- `athlete`: capacidad vigente `calendar.sport.view` consultada en Afiliación (HU035).
- `organizer`: permiso `events.calendar.view` del contrato de permisos (HU023), sin Afiliación.
- Otro actor: `permission_required`.
- Eventos `PUBLISHED` cuyo intervalo `[starts_at, ends_at]` se superpone con `[from, to]`;
  nunca borradores. Filtros opcionales `discipline` y `city` por igualdad normalizada
  (sin mayúsculas ni acentos, recortados). Orden `starts_at`, `event_id` con cursor keyset
  opaco; el cursor solo marca la posición y se reenvía con los mismos filtros.
- Periodo: como máximo 92 días; `to` anterior a `from` es inválido y, para el organizador,
  también `from == to` (decisión del responsable del 2026-10-20: ADR-040 rige para HU023 y
  el deportista conserva ADR-031).
- Lee la base de lectura; un evento recién publicado aparece cuando se proyecta.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from events_management.application.errors import CapabilityRequired, PermissionRequired
from events_management.application.ports import (
    CalendarEventView,
    Caller,
    CapabilityChecker,
    EventPosition,
    PermissionPolicy,
    PublishedEventReader,
    PublishedEventsQuery,
)
from events_management.domain.sport_events.model import DISCIPLINE_MAX, DISCIPLINE_MIN
from events_management.domain.sport_events.terms import normalize_term

ATHLETE = "athlete"
ORGANIZER = "organizer"
CALENDAR_CAPABILITY = "calendar.sport.view"
ORGANIZER_CALENDAR_PERMISSION = "events.calendar.view"
MAX_LIMIT = 200
DEFAULT_LIMIT = 200
MAX_WINDOW = timedelta(days=92)
# Mismos límites que el texto del evento (ADR-028): disciplina y ciudad de 2 a 80 caracteres.
TERM_MIN, TERM_MAX = DISCIPLINE_MIN, DISCIPLINE_MAX


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
    discipline: str | None = None
    city: str | None = None


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


def _term(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    normalized = normalize_term(value)
    if not TERM_MIN <= len(normalized) <= TERM_MAX:
        raise InvalidEventsQuery(field, "invalid_length")
    return normalized


class ListPublishedEvents:
    def __init__(
        self,
        *,
        reader: PublishedEventReader,
        capabilities: CapabilityChecker,
        permissions: PermissionPolicy,
    ) -> None:
        self._reader = reader
        self._capabilities = capabilities
        self._permissions = permissions

    def validate(
        self, filters: EventsFilters, *, strict_period: bool = False
    ) -> EventPosition | None:
        """Periodo, límite, cursor y filtros; `strict_period` rechaza también `from == to`."""
        for field, value in (("from", filters.period_from), ("to", filters.period_to)):
            if value.tzinfo is None:
                raise InvalidEventsQuery(field, "invalid_datetime")
        if filters.period_to < filters.period_from or (
            strict_period and filters.period_to == filters.period_from
        ):
            raise InvalidEventsQuery("to", "ends_before_starts")
        if filters.period_to - filters.period_from > MAX_WINDOW:
            raise InvalidEventsQuery("to", "out_of_range")
        if not 1 <= filters.limit <= MAX_LIMIT:
            raise InvalidEventsQuery("limit", "out_of_range")
        _term(filters.discipline, "discipline")
        _term(filters.city, "city")
        return decode_cursor(filters.cursor) if filters.cursor else None

    def __call__(self, caller: Caller, filters: EventsFilters) -> EventsPage:
        organizer = caller.actor_type == ORGANIZER
        if organizer:
            if not self._permissions.allows(caller.actor_type, ORGANIZER_CALENDAR_PERMISSION):
                raise PermissionRequired()
        elif caller.actor_type != ATHLETE:
            raise PermissionRequired()
        after = self.validate(filters, strict_period=organizer)
        # Afiliación se consulta fuera de cualquier transacción; nunca se concede por suposición.
        if not organizer and not self._capabilities.has_capability(
            caller.user_id, CALENDAR_CAPABILITY
        ):
            raise CapabilityRequired()
        # Se pide uno de más para saber si existe una página siguiente.
        events = self._reader.published(
            PublishedEventsQuery(
                starts_before_or_at=filters.period_to,
                ends_at_or_after=filters.period_from,
                after=after,
                limit=filters.limit + 1,
                discipline=_term(filters.discipline, "discipline"),
                city=_term(filters.city, "city"),
            )
        )
        page = events[: filters.limit]
        next_cursor = (
            encode_cursor(EventPosition(page[-1].starts_at, page[-1].event_id))
            if len(events) > filters.limit
            else None
        )
        return EventsPage(items=tuple(page), next_cursor=next_cursor)
