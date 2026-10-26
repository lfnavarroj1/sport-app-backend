"""HU022: inscritos de un evento propio del organizador (ADR-039), sobre la base de lectura.

- Solo `organizer` con el permiso por actor `events.attendees.view` (ADR-025); otro actor o
  sin el permiso → `permission_required`.
- El evento debe ser propio: uno ajeno o inexistente es `event_not_found`, sin revelar
  participantes (CA2).
- Lee `event_registration_read` (ADR-032): `registration_id`, `athlete_id`, `status`,
  `registered_at` y `applied_fee`, en orden `registered_at`, `registration_id` ascendente con
  cursor keyset opaco (CA4); `limit` de 1 a 100.
- `status`: `REGISTERED` por defecto o `ALL` (sin filtro). `by_status` cuenta todas las
  inscripciones del evento por estado y `total` es el número de inscripciones que cumplen el
  filtro; ambos salen de la misma lectura que la página. Sin inscritos: lista vacía y
  `total = 0` (CA3).
- Cada consulta se audita (`audit.attendees_access`) con el organizador, el evento y el
  resultado, nunca con datos de los inscritos.
"""

import base64
import binascii
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from events_management.application.errors import PermissionRequired
from events_management.application.ports import (
    AttendeePage,
    AttendeePosition,
    AttendeeReader,
    AttendeesQuery,
    Caller,
    PermissionPolicy,
)
from events_management.domain.registrations.model import EventNotAvailable, RegistrationStatus

ORGANIZER = "organizer"
ATTENDEES_PERMISSION = "events.attendees.view"
ALL_STATUSES = "ALL"
DEFAULT_STATUS = RegistrationStatus.REGISTERED.value
MAX_LIMIT = 100
DEFAULT_LIMIT = 20

_audit = logging.getLogger("sportapp.audit")


class InvalidAttendeesQuery(Exception):
    code = "validation_error"

    def __init__(self, field: str, issue: str) -> None:
        super().__init__(f"{field}: {issue}")
        self.field = field
        self.issue = issue


@dataclass(frozen=True)
class AttendeesFilters:
    status: str | None = None
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None


@dataclass(frozen=True)
class AttendeesResult:
    page: AttendeePage
    total: int
    next_cursor: str | None


def encode_cursor(position: AttendeePosition) -> str:
    raw = json.dumps(
        {"t": position.registered_at.isoformat(), "r": str(position.registration_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> AttendeePosition:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        registered_at = datetime.fromisoformat(document["t"])
        registration_id = UUID(document["r"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        raise InvalidAttendeesQuery("cursor", "invalid_cursor") from None
    if registered_at.tzinfo is None:
        raise InvalidAttendeesQuery("cursor", "invalid_cursor")
    return AttendeePosition(registered_at=registered_at, registration_id=registration_id)


def _status_filter(raw: str | None) -> str | None:
    """`None` → `REGISTERED`; `ALL` → sin filtro; otro valor debe ser un estado conocido."""
    if raw is None:
        return DEFAULT_STATUS
    if raw == ALL_STATUSES:
        return None
    if raw not in {status.value for status in RegistrationStatus}:
        raise InvalidAttendeesQuery("status", "unknown_value")
    return raw


class ListEventAttendees:
    def __init__(self, *, reader: AttendeeReader, permissions: PermissionPolicy) -> None:
        self._reader = reader
        self._permissions = permissions

    def __call__(
        self, caller: Caller, event_id: UUID, filters: AttendeesFilters
    ) -> AttendeesResult:
        try:
            result = self._list(caller, event_id, filters)
        except (PermissionRequired, EventNotAvailable, InvalidAttendeesQuery) as error:
            self._audit(caller, event_id, "rejected", reason=error.code)
            raise
        self._audit(caller, event_id, "listed", returned=len(result.page.items))
        return result

    def _list(self, caller: Caller, event_id: UUID, filters: AttendeesFilters) -> AttendeesResult:
        if caller.actor_type != ORGANIZER or not self._permissions.allows(
            caller.actor_type, ATTENDEES_PERMISSION
        ):
            raise PermissionRequired()
        status = _status_filter(filters.status)
        if not 1 <= filters.limit <= MAX_LIMIT:
            raise InvalidAttendeesQuery("limit", "out_of_range")
        after = decode_cursor(filters.cursor) if filters.cursor else None
        # Se pide uno de más para saber si existe una página siguiente.
        page = self._reader.attendees(
            AttendeesQuery(
                event_id=event_id,
                organizer_id=caller.user_id,
                status=status,
                after=after,
                limit=filters.limit + 1,
            )
        )
        if page is None:
            raise EventNotAvailable()
        items = page.items[: filters.limit]
        next_cursor = (
            encode_cursor(AttendeePosition(items[-1].registered_at, items[-1].registration_id))
            if len(page.items) > filters.limit
            else None
        )
        total = (
            page.by_status.get(status, 0) if status is not None else sum(page.by_status.values())
        )
        return AttendeesResult(
            page=AttendeePage(items=items, by_status=page.by_status),
            total=total,
            next_cursor=next_cursor,
        )

    @staticmethod
    def _audit(
        caller: Caller,
        event_id: UUID,
        result: str,
        *,
        reason: str | None = None,
        returned: int | None = None,
    ) -> None:
        fields: dict[str, object] = {
            "organizer_id": str(caller.user_id),
            "event_id": str(event_id),
            "result": result,
        }
        if reason is not None:
            fields["reason"] = reason
        if returned is not None:
            fields["returned"] = returned
        _audit.info("audit.attendees_access", extra={"fields": fields})
