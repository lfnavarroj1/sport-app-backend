"""Evento masivo en borrador (HU021, ADR-028).

- Textos recortados: nombre de 3 a 120 caracteres, descripción de 20 a 2.000 y
  disciplina de 2 a 80 (texto libre en el MVP, sin catálogo administrable).
- Ubicación estructurada: nombre de 3 a 120, dirección de 5 a 200, ciudad de 2 a 80 y
  país ISO 3166-1 alfa-2 en mayúsculas.
- Inicio futuro con desfase UTC explícito, coherente con la zona IANA declarada en ese
  instante; duración de 15 a 1.440 minutos y capacidad de 1 a 100.000.
- El fin lo calcula el backend; inicio y fin se normalizan a UTC sin perder la zona.
- Nace en `DRAFT`, versión 1, del organizador autenticado.

HU024 (ADR-033): la única transición es `DRAFT` → `PUBLISHED`. Incrementa una vez la
versión, fija `published_at` con el reloj del servidor y exige que el agregado persistido
cumpla de nuevo todas las reglas de ADR-028, con `starts_at` posterior a la publicación.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from events_management.domain.sport_events.countries import COUNTRY_CODES
from events_management.domain.validation import InvalidSportEvent, Issue, Violation

NAME_MIN, NAME_MAX = 3, 120
DESCRIPTION_MIN, DESCRIPTION_MAX = 20, 2000
DISCIPLINE_MIN, DISCIPLINE_MAX = 2, 80
LOCATION_NAME_MIN, LOCATION_NAME_MAX = 3, 120
ADDRESS_MIN, ADDRESS_MAX = 5, 200
CITY_MIN, CITY_MAX = 2, 80
DURATION_MIN, DURATION_MAX = 15, 1440
CAPACITY_MIN, CAPACITY_MAX = 1, 100_000
INITIAL_VERSION = 1


class EventStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"


class EventNotFound(Exception):
    """El evento no existe o pertenece a otro organizador; no se revela cuál (ADR-033)."""

    code = "event_not_found"


@dataclass(frozen=True)
class LocationInput:
    """Ubicación tal como llega; `None` si el campo faltaba o tenía otro tipo JSON."""

    name: str | None
    address: str | None
    city: str | None
    country_code: str | None


@dataclass(frozen=True)
class EventInput:
    """Solicitud tal como llega, con las violaciones de forma ya detectadas por el transporte."""

    name: str | None
    description: str | None
    discipline: str | None
    location: LocationInput | None
    starts_at: str | None
    time_zone: str | None
    duration_minutes: int | None
    capacity: int | None
    transport_violations: tuple[Violation, ...] = ()


@dataclass(frozen=True)
class Location:
    name: str
    address: str
    city: str
    country_code: str


@dataclass(frozen=True)
class EventDetails:
    """Datos normalizados del evento; instantes en UTC."""

    name: str
    description: str
    discipline: str
    location: Location
    starts_at: datetime
    ends_at: datetime
    time_zone: str
    duration_minutes: int
    capacity: int


@dataclass(frozen=True)
class SportEvent:
    event_id: UUID
    organizer_id: UUID
    details: EventDetails
    status: EventStatus
    version: int
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None = None

    @classmethod
    def draft(
        cls, *, event_id: UUID, organizer_id: UUID, details: EventDetails, now: datetime
    ) -> "SportEvent":
        created_at = now.astimezone(UTC)
        return cls(
            event_id=event_id,
            organizer_id=organizer_id,
            details=details,
            status=EventStatus.DRAFT,
            version=INITIAL_VERSION,
            created_at=created_at,
            updated_at=created_at,
        )

    def publish(self, now: datetime) -> "SportEvent":
        """`DRAFT` → `PUBLISHED`: una sola subida de versión y `published_at` del servidor.

        Revalida el agregado con ADR-028 en el instante de la publicación; si incumple
        alguna regla lanza `InvalidSportEvent` y el evento sigue `DRAFT`.
        """
        if self.status is not EventStatus.DRAFT:
            raise ValueError("Solo un borrador se puede publicar.")
        revalidate_for_publication(self.details, now)
        published_at = now.astimezone(UTC)
        return replace(
            self,
            status=EventStatus.PUBLISHED,
            version=self.version + 1,
            updated_at=published_at,
            published_at=published_at,
        )


def _text(
    value: str | None, field: str, bounds: tuple[int, int], violations: list[Violation]
) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    if not bounds[0] <= len(trimmed) <= bounds[1]:
        violations.append(Violation(field, Issue.INVALID_LENGTH))
        return None
    return trimmed


def _bounded(
    value: int | None, field: str, bounds: tuple[int, int], violations: list[Violation]
) -> int | None:
    if value is None:
        return None
    if not bounds[0] <= value <= bounds[1]:
        violations.append(Violation(field, Issue.OUT_OF_RANGE))
        return None
    return value


def _location(raw: LocationInput | None, violations: list[Violation]) -> Location | None:
    if raw is None:
        return None
    name = _text(raw.name, "location.name", (LOCATION_NAME_MIN, LOCATION_NAME_MAX), violations)
    address = _text(raw.address, "location.address", (ADDRESS_MIN, ADDRESS_MAX), violations)
    city = _text(raw.city, "location.city", (CITY_MIN, CITY_MAX), violations)
    country = raw.country_code
    if country is not None and country not in COUNTRY_CODES:
        violations.append(Violation("location.country_code", Issue.UNKNOWN_VALUE))
        country = None
    if name is None or address is None or city is None or country is None:
        return None
    return Location(name=name, address=address, city=city, country_code=country)


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


def _schedule(
    raw: EventInput, now: datetime, violations: list[Violation]
) -> tuple[datetime, str] | None:
    starts = _instant(raw.starts_at) if raw.starts_at is not None else None
    if raw.starts_at is not None and starts is None:
        violations.append(Violation("starts_at", Issue.UNKNOWN_VALUE))
    zone = _zone(raw.time_zone) if raw.time_zone is not None else None
    if raw.time_zone is not None and zone is None:
        violations.append(Violation("time_zone", Issue.TIME_ZONE_UNKNOWN))
    if starts is not None and starts <= now:
        violations.append(Violation("starts_at", Issue.NOT_IN_FUTURE))
        return None
    if starts is None or zone is None or raw.time_zone is None:
        return None
    if starts.utcoffset() != starts.astimezone(zone).utcoffset():
        violations.append(Violation("time_zone", Issue.TIME_ZONE_OFFSET_MISMATCH))
        return None
    return starts.astimezone(UTC), raw.time_zone


def validate_event(raw: EventInput, now: datetime) -> EventDetails:
    """Datos normalizados o `InvalidSportEvent` con todas las reglas incumplidas."""
    violations = list(raw.transport_violations)
    name = _text(raw.name, "name", (NAME_MIN, NAME_MAX), violations)
    description = _text(
        raw.description, "description", (DESCRIPTION_MIN, DESCRIPTION_MAX), violations
    )
    discipline = _text(raw.discipline, "discipline", (DISCIPLINE_MIN, DISCIPLINE_MAX), violations)
    location = _location(raw.location, violations)
    schedule = _schedule(raw, now, violations)
    duration = _bounded(
        raw.duration_minutes, "duration_minutes", (DURATION_MIN, DURATION_MAX), violations
    )
    capacity = _bounded(raw.capacity, "capacity", (CAPACITY_MIN, CAPACITY_MAX), violations)
    if (
        violations
        or name is None
        or description is None
        or discipline is None
        or location is None
        or schedule is None
        or duration is None
        or capacity is None
    ):
        raise InvalidSportEvent(violations)
    starts_at, time_zone = schedule
    return EventDetails(
        name=name,
        description=description,
        discipline=discipline,
        location=location,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(minutes=duration),
        time_zone=time_zone,
        duration_minutes=duration,
        capacity=capacity,
    )


def revalidate_for_publication(details: EventDetails, now: datetime) -> None:
    """Reglas de ADR-028 sobre el agregado persistido más `starts_at` futuro (CA2).

    El inicio se expresa en su zona declarada, como lo habría enviado el organizador, para
    que también se compruebe que la zona sigue existiendo y coincide con el desfase.
    """
    zone = _zone(details.time_zone)
    starts_at = details.starts_at.astimezone(zone) if zone is not None else details.starts_at
    location = details.location
    validate_event(
        EventInput(
            name=details.name,
            description=details.description,
            discipline=details.discipline,
            location=LocationInput(
                name=location.name,
                address=location.address,
                city=location.city,
                country_code=location.country_code,
            ),
            starts_at=starts_at.isoformat(),
            time_zone=details.time_zone,
            duration_minutes=details.duration_minutes,
            capacity=details.capacity,
        ),
        now,
    )
