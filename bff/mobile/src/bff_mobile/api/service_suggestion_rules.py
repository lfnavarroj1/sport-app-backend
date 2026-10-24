"""Reglas de las sugerencias de acompañamiento y logística (HU029, ADR-041), sin transporte.

- Ventana: desde ahora hasta 31 días después (el máximo de `GET /v1/services/availability`).
- `EVENT_LOGISTICS`: servicio `TRANSPORT` o `ACCOMPANIMENT` con un intervalo `[starts_at,
  ends_at)` que se solapa con algún **día** de un evento inscrito. El día se calcula en la
  `time_zone` del evento: desde la medianoche local de su fecha de inicio hasta la medianoche
  local siguiente a su fecha de fin (un evento de varios días cubre todos sus días).
- `UPCOMING_AVAILABILITY`: servicio `COACHING` con al menos un intervalo en la ventana.
- Cualquier otra categoría (en particular `SPORTS_MEDICINE`) nunca se sugiere; un servicio de
  transporte o acompañamiento sin coincidencia tampoco (no hay servicios arbitrarios).
- `next_interval`: el intervalo que justifica el motivo (el primero que coincide con un día de
  evento, o el primero disponible para `COACHING`).
- Orden determinista: `EVENT_LOGISTICS` primero, luego el inicio de `next_interval` y, ante
  empate, `service_id`.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

WINDOW = timedelta(days=31)
SUGGESTED_CATEGORIES = ("COACHING", "TRANSPORT", "ACCOMPANIMENT")
LOGISTICS_CATEGORIES = frozenset({"TRANSPORT", "ACCOMPANIMENT"})
COACHING = "COACHING"
EVENT_LOGISTICS = "EVENT_LOGISTICS"
UPCOMING_AVAILABILITY = "UPCOMING_AVAILABILITY"


@dataclass(frozen=True)
class EventDays:
    """Días locales de un evento inscrito, como rango `[starts_at, ends_at)` en UTC."""

    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class Interval:
    starts_at: datetime
    ends_at: datetime
    time_zone: str


@dataclass(frozen=True)
class Suggestion:
    service: dict[str, Any]
    reason: str
    interval: Interval


def instant(value: str) -> datetime:
    """Instante ISO 8601 con desfase (los contratos usan el sufijo `Z`)."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("instante sin desfase")
    return parsed.astimezone(UTC)


def event_days(starts_at: datetime, ends_at: datetime, time_zone: str) -> EventDays:
    zone = ZoneInfo(time_zone)
    first = starts_at.astimezone(zone).date()
    # El fin es exclusivo: un evento que termina a medianoche no ocupa el día siguiente.
    last = max(first, (ends_at - timedelta(microseconds=1)).astimezone(zone).date())
    return EventDays(
        starts_at=datetime.combine(first, time(), zone).astimezone(UTC),
        ends_at=datetime.combine(last + timedelta(days=1), time(), zone).astimezone(UTC),
    )


def overlaps(interval: Interval, days: EventDays) -> bool:
    return interval.starts_at < days.ends_at and interval.ends_at > days.starts_at


def _intervals(service: dict[str, Any]) -> list[Interval]:
    intervals = [
        Interval(instant(item["starts_at"]), instant(item["ends_at"]), str(item["time_zone"]))
        for item in service["availability"]
    ]
    return sorted(intervals, key=lambda interval: (interval.starts_at, interval.ends_at))


def suggest(services: Iterable[dict[str, Any]], registered: list[EventDays]) -> list[Suggestion]:
    """Servicios sugeridos y su motivo, en el orden de la respuesta."""
    suggestions: list[Suggestion] = []
    for service in services:
        intervals = _intervals(service)
        if not service["available"] or not intervals:
            continue
        category = service["category"]
        if category in LOGISTICS_CATEGORIES:
            matched = next(
                (
                    interval
                    for interval in intervals
                    if any(overlaps(interval, days) for days in registered)
                ),
                None,
            )
            if matched is not None:
                suggestions.append(Suggestion(service, EVENT_LOGISTICS, matched))
        elif category == COACHING:
            suggestions.append(Suggestion(service, UPCOMING_AVAILABILITY, intervals[0]))
    return sorted(
        suggestions,
        key=lambda item: (
            item.reason != EVENT_LOGISTICS,
            item.interval.starts_at,
            str(item.service["service_id"]),
        ),
    )
