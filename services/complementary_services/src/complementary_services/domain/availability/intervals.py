"""Disponibilidad por intervalos explícitos (ADR-027 §9-10).

- Hasta 100 intervalos; cada uno con inicio y fin con desfase UTC, zona IANA y
  capacidad de 1 a 100.
- El inicio está en el futuro; la duración es mayor que cero y no supera 24 horas;
  el fin no pasa de los siguientes 365 días.
- El desfase de cada instante coincide con el de la zona declarada en ese instante.
- Intervalos semiabiertos `[inicio, fin)`: contiguos sí, superpuestos no.
- Los instantes se normalizan a UTC sin perder la zona declarada.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from complementary_services.domain.validation import Issue, Violation

MAX_INTERVALS = 100
CAPACITY_MIN = 1
CAPACITY_MAX = 100
MAX_DURATION = timedelta(hours=24)
HORIZON = timedelta(days=365)


@dataclass(frozen=True)
class IntervalInput:
    """Intervalo tal como llega; `None` si el campo faltaba o tenía otro tipo JSON."""

    starts_at: str | None
    ends_at: str | None
    time_zone: str | None
    capacity: int | None


@dataclass(frozen=True)
class AvailabilityInterval:
    starts_at: datetime  # UTC
    ends_at: datetime  # UTC
    time_zone: str
    capacity: int
    # HU040 (ADR-046): lo asigna la base al guardar el intervalo; `None` antes de persistirlo.
    availability_id: UUID | None = None


def _instant(value: str) -> datetime | None:
    """ISO 8601 con desfase obligatorio; `None` si no cumple el formato."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _zone(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _offset_matches(instant: datetime, zone: ZoneInfo) -> bool:
    return instant.utcoffset() == instant.astimezone(zone).utcoffset()


def _interval(
    raw: IntervalInput, index: int, now: datetime
) -> tuple[AvailabilityInterval | None, list[Violation]]:
    path = f"availability[{index}]"
    violations: list[Violation] = []
    starts = _instant(raw.starts_at) if raw.starts_at is not None else None
    ends = _instant(raw.ends_at) if raw.ends_at is not None else None
    if raw.starts_at is not None and starts is None:
        violations.append(Violation(f"{path}.starts_at", Issue.UNKNOWN_VALUE))
    if raw.ends_at is not None and ends is None:
        violations.append(Violation(f"{path}.ends_at", Issue.UNKNOWN_VALUE))
    zone = _zone(raw.time_zone) if raw.time_zone is not None else None
    if raw.time_zone is not None and zone is None:
        violations.append(Violation(f"{path}.time_zone", Issue.TIME_ZONE_UNKNOWN))
    if raw.capacity is not None and not CAPACITY_MIN <= raw.capacity <= CAPACITY_MAX:
        violations.append(Violation(f"{path}.capacity", Issue.OUT_OF_RANGE))
    if zone is not None and any(
        instant is not None and not _offset_matches(instant, zone) for instant in (starts, ends)
    ):
        violations.append(Violation(f"{path}.time_zone", Issue.TIME_ZONE_OFFSET_MISMATCH))
    if starts is not None and starts <= now:
        violations.append(Violation(f"{path}.starts_at", Issue.NOT_IN_FUTURE))
    if starts is not None and ends is not None:
        if ends <= starts:
            violations.append(Violation(f"{path}.ends_at", Issue.ENDS_BEFORE_STARTS))
        elif ends - starts > MAX_DURATION:
            violations.append(Violation(f"{path}.ends_at", Issue.OUT_OF_RANGE))
    if ends is not None and ends > now + HORIZON:
        violations.append(Violation(f"{path}.ends_at", Issue.OUT_OF_RANGE))
    if violations or starts is None or ends is None or zone is None or raw.capacity is None:
        return None, violations
    interval = AvailabilityInterval(
        starts_at=starts.astimezone(UTC),
        ends_at=ends.astimezone(UTC),
        time_zone=raw.time_zone or "",
        capacity=raw.capacity,
    )
    return interval, []


def validate_availability(
    raw: list[IntervalInput], now: datetime
) -> tuple[tuple[AvailabilityInterval, ...], list[Violation]]:
    """Intervalos normalizados ordenados por inicio, o las reglas incumplidas."""
    if len(raw) > MAX_INTERVALS:
        return (), [Violation("availability", Issue.TOO_MANY_INTERVALS)]
    violations: list[Violation] = []
    valid: list[tuple[int, AvailabilityInterval]] = []
    for index, item in enumerate(raw):
        interval, problems = _interval(item, index, now)
        violations.extend(problems)
        if interval is not None:
            valid.append((index, interval))
    ordered = sorted(valid, key=lambda pair: (pair[1].starts_at, pair[0]))
    latest_end: datetime | None = None
    for index, current in ordered:
        # Semiabiertos: empezar justo cuando termina otro no es superposición.
        if latest_end is not None and current.starts_at < latest_end:
            violations.append(Violation(f"availability[{index}]", Issue.OVERLAPPING_INTERVAL))
        latest_end = current.ends_at if latest_end is None else max(latest_end, current.ends_at)
    if violations:
        return (), violations
    return tuple(interval for _, interval in ordered), []
