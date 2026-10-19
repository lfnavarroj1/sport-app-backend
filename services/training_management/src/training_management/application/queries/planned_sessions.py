"""HU035: sesiones planificadas del deportista para su calendario (ADR-031).

- Solo `athlete`; el deportista sale del token y no se exige capacidad para leer las
  sesiones propias (misma regla que el historial propio de HU011).
- Sesiones de planes `ASSIGNED` con `scheduled_date` entre `from` y `to` inclusive,
  ordenadas por `scheduled_date`, `plan_id`, `session_id`, con cursor keyset opaco.
- Lee la base de lectura; un plan recién asignado aparece cuando se proyecta.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from training_management.application.access import require_athlete
from training_management.application.ports import (
    Caller,
    PlannedSessionEntry,
    PlannedSessionPosition,
    PlannedSessionQuery,
    PlannedSessionReader,
)

MAX_LIMIT = 200
DEFAULT_LIMIT = 200


class InvalidPlannedSessionQuery(Exception):
    """Rango, límite o cursor inválido; se informa el parámetro afectado."""

    code = "validation_error"

    def __init__(self, field: str, issue: str) -> None:
        super().__init__(f"{field}: {issue}")
        self.field = field
        self.issue = issue


@dataclass(frozen=True)
class PlannedSessionFilters:
    date_from: date
    date_to: date
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None


@dataclass(frozen=True)
class PlannedSessionPage:
    items: tuple[PlannedSessionEntry, ...]
    next_cursor: str | None


def encode_cursor(position: PlannedSessionPosition) -> str:
    raw = json.dumps(
        {
            "d": position.scheduled_date.isoformat(),
            "p": str(position.plan_id),
            "s": str(position.session_id),
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> PlannedSessionPosition:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        return PlannedSessionPosition(
            scheduled_date=date.fromisoformat(document["d"]),
            plan_id=UUID(document["p"]),
            session_id=UUID(document["s"]),
        )
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        raise InvalidPlannedSessionQuery("cursor", "invalid_cursor") from None


class GetPlannedSessions:
    def __init__(self, *, reader: PlannedSessionReader) -> None:
        self._reader = reader

    def own(self, caller: Caller, filters: PlannedSessionFilters) -> PlannedSessionPage:
        require_athlete(caller)
        if not 1 <= filters.limit <= MAX_LIMIT:
            raise InvalidPlannedSessionQuery("limit", "out_of_range")
        if filters.date_to < filters.date_from:
            raise InvalidPlannedSessionQuery("to", "ends_before_starts")
        after = decode_cursor(filters.cursor) if filters.cursor else None
        # Se pide uno de más para saber si existe una página siguiente.
        entries = self._reader.list(
            PlannedSessionQuery(
                athlete_id=caller.user_id,
                date_from=filters.date_from,
                date_to=filters.date_to,
                after=after,
                limit=filters.limit + 1,
            )
        )
        page = entries[: filters.limit]
        next_cursor = (
            encode_cursor(
                PlannedSessionPosition(
                    page[-1].scheduled_date, page[-1].plan_id, page[-1].session_id
                )
            )
            if len(entries) > filters.limit
            else None
        )
        return PlannedSessionPage(items=tuple(page), next_cursor=next_cursor)
