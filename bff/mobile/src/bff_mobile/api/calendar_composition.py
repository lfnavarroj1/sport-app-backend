"""Reglas de composición del calendario del deportista (HU035, ADR-031), sin transporte.

- Periodo: `from`/`to` instantes ISO 8601 con desfase; ambos ausentes → mes UTC en curso
  (`to` es el último segundo del mes); `from` ≤ `to`; ventana de hasta 92 días.
- Las sesiones planificadas se piden por fecha: se usan las fechas de `from` y `to` en el
  desfase con que llegaron (la zona del dispositivo), o en UTC si se tomó el mes por defecto.
- `occurs_on`: fecha del evento en su propia `time_zone`, o la `scheduled_date` de la sesión.
- Orden: `occurs_on`, `starts_at` (nulos primero), `source`, `item_id`.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_WINDOW = timedelta(days=92)
EVENT_SOURCE = "EVENT_PUBLISHED"
SESSION_SOURCE = "TRAINING_PLAN_SESSION"


class InvalidPeriod(Exception):
    code = "validation_error"

    def __init__(self, field: str, issue: str) -> None:
        super().__init__(f"{field}: {issue}")
        self.field = field
        self.issue = issue


@dataclass(frozen=True)
class Period:
    period_from: datetime  # con el desfase recibido
    period_to: datetime

    @property
    def date_from(self) -> date:
        return self.period_from.date()

    @property
    def date_to(self) -> date:
        return self.period_to.date()


def _instant(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise InvalidPeriod(field, "invalid_datetime") from None
    if parsed.tzinfo is None:
        raise InvalidPeriod(field, "invalid_datetime")
    return parsed


def current_month(now: datetime) -> Period:
    start = now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (start + timedelta(days=32)).replace(day=1)
    return Period(start, next_month - timedelta(seconds=1))


def normalize_period(raw_from: str | None, raw_to: str | None, now: datetime) -> Period:
    if raw_from is None and raw_to is None:
        return current_month(now)
    if raw_from is None or raw_to is None:
        raise InvalidPeriod("from" if raw_from is None else "to", "required")
    period = Period(_instant(raw_from, "from"), _instant(raw_to, "to"))
    if period.period_to < period.period_from:
        raise InvalidPeriod("to", "ends_before_starts")
    if period.period_to - period.period_from > MAX_WINDOW:
        raise InvalidPeriod("to", "out_of_range")
    return period


def utc_instant(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _local_date(instant: datetime, time_zone: str) -> date:
    try:
        return instant.astimezone(ZoneInfo(time_zone)).date()
    except (ZoneInfoNotFoundError, ValueError):
        return instant.astimezone(UTC).date()


def event_item(event: dict[str, Any]) -> dict[str, Any]:
    starts_at = datetime.fromisoformat(str(event["starts_at"]))
    ends_at = datetime.fromisoformat(str(event["ends_at"]))
    time_zone = str(event["time_zone"])
    location = event["location"]
    return {
        "item_id": f"{EVENT_SOURCE}:{event['event_id']}",
        "source": EVENT_SOURCE,
        "occurs_on": _local_date(starts_at, time_zone).isoformat(),
        "title": event["name"],
        "sport": event["discipline"],
        "starts_at": utc_instant(starts_at),
        "ends_at": utc_instant(ends_at),
        "time_zone": time_zone,
        "location": {
            "name": location["name"],
            "city": location["city"],
            "country_code": location["country_code"],
        },
        "reference": {"event_id": event["event_id"]},
    }


def session_item(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_id": f"{SESSION_SOURCE}:{session['session_id']}",
        "source": SESSION_SOURCE,
        "occurs_on": session["scheduled_date"],
        "title": session["plan_title"],
        "sport": session["sport"],
        "starts_at": None,
        "ends_at": None,
        "time_zone": None,
        "duration_minutes": session["duration_minutes"],
        "rpe": session["rpe"],
        "reference": {"plan_id": session["plan_id"], "session_id": session["session_id"]},
    }


_NO_TIME = datetime.min.replace(tzinfo=UTC)


def _order(item: dict[str, Any]) -> tuple[str, int, datetime, str, str]:
    starts_at = item["starts_at"]
    # Nulos primero: una sesión sin hora va antes que los eventos del mismo día.
    return (
        item["occurs_on"],
        0 if starts_at is None else 1,
        _NO_TIME if starts_at is None else datetime.fromisoformat(starts_at),
        item["source"],
        item["item_id"],
    )


def ordered(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=_order)
