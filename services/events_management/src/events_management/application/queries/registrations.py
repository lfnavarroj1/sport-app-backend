"""HU041: consultas del deportista sobre la base de lectura (ADR-032).

- Detalle de un evento `PUBLISHED` con `my_registration` del deportista; un evento
  inexistente o en borrador es `event_not_found` (CA2).
- «Mis inscripciones»: solo las propias, ordenadas por `event.starts_at`, `registration_id`
  con cursor keyset y filtro opcional `status` (CA8).
Solo `athlete` (CA9); sin capacidad de plan.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from events_management.application.errors import PermissionRequired
from events_management.application.ports import (
    Caller,
    EventDetailReader,
    EventDetailView,
    RegistrationPosition,
    RegistrationReader,
    RegistrationsQuery,
    RegistrationView,
)
from events_management.domain.registrations.model import EventNotAvailable, RegistrationStatus

ATHLETE = "athlete"
MAX_LIMIT = 100
DEFAULT_LIMIT = 20


class InvalidRegistrationsQuery(Exception):
    code = "validation_error"

    def __init__(self, field: str, issue: str) -> None:
        super().__init__(f"{field}: {issue}")
        self.field = field
        self.issue = issue


@dataclass(frozen=True)
class RegistrationsFilters:
    status: str | None = None
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None


@dataclass(frozen=True)
class RegistrationsPage:
    items: tuple[RegistrationView, ...]
    next_cursor: str | None


def encode_cursor(position: RegistrationPosition) -> str:
    raw = json.dumps(
        {"t": position.starts_at.isoformat(), "r": str(position.registration_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> RegistrationPosition:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        starts_at = datetime.fromisoformat(document["t"])
        registration_id = UUID(document["r"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        raise InvalidRegistrationsQuery("cursor", "invalid_cursor") from None
    if starts_at.tzinfo is None:
        raise InvalidRegistrationsQuery("cursor", "invalid_cursor")
    return RegistrationPosition(starts_at=starts_at, registration_id=registration_id)


def _require_athlete(caller: Caller) -> None:
    if caller.actor_type != ATHLETE:
        raise PermissionRequired()


class GetPublishedEvent:
    def __init__(self, *, reader: EventDetailReader) -> None:
        self._reader = reader

    def __call__(self, caller: Caller, event_id: UUID) -> EventDetailView:
        """ADR-033: un evento `PUBLISHED` es visible para cualquier actor autenticado;
        `my_registration` solo se compone para el deportista (ADR-032). El `DRAFT` visible para
        su organizador propietario llega con HU024."""
        athlete_id = caller.user_id if caller.actor_type == ATHLETE else None
        detail = self._reader.published_detail(event_id, athlete_id)
        if detail is None:
            raise EventNotAvailable()
        return detail


class ListMyRegistrations:
    def __init__(self, *, reader: RegistrationReader) -> None:
        self._reader = reader

    def __call__(self, caller: Caller, filters: RegistrationsFilters) -> RegistrationsPage:
        _require_athlete(caller)
        if filters.status is not None and filters.status not in {
            s.value for s in RegistrationStatus
        }:
            raise InvalidRegistrationsQuery("status", "unknown_value")
        if not 1 <= filters.limit <= MAX_LIMIT:
            raise InvalidRegistrationsQuery("limit", "out_of_range")
        after = decode_cursor(filters.cursor) if filters.cursor else None
        items = self._reader.list(
            RegistrationsQuery(
                athlete_id=caller.user_id,
                status=filters.status,
                after=after,
                limit=filters.limit + 1,
            )
        )
        page = items[: filters.limit]
        next_cursor = (
            encode_cursor(
                RegistrationPosition(
                    page[-1].event.starts_at, page[-1].registration.registration_id
                )
            )
            if len(items) > filters.limit
            else None
        )
        return RegistrationsPage(items=tuple(page), next_cursor=next_cursor)
