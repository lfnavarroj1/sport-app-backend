"""Sección de hábitos deportivos del perfil (HU006, ADR-034).

De uno a cinco deportes del catálogo de HU010, cada uno con frecuencia semanal, duración
típica, intensidad, nivel y años de práctica opcionales; disponibilidad por días y franjas
y objetivo principal opcional. Todos los catálogos son cerrados.

Normalización canónica: los deportes, los días y las franjas se devuelven en el orden de
su catálogo, sea cual sea el orden recibido. Así la misma información produce el mismo
documento y la misma huella idempotente. Los errores nombran la posición recibida
(`sports[1].sport`, `availability.days[0]`).
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from users_management.domain.profile.validation import (
    Collector,
    InvalidProfileSection,
    Issue,
    Violation,
)

SPORTS_MAX = 5
FREQUENCY_MIN, FREQUENCY_MAX = 1, 7
DURATION_MIN, DURATION_MAX = 10, 600
YEARS_MIN, YEARS_MAX = 0, 80
DAYS_MAX = 7
TIME_SLOTS_MAX = 4


class Sport(StrEnum):
    """Catálogo de deportes de HU010."""

    RUNNING = "running"
    CYCLING = "cycling"
    SWIMMING = "swimming"
    STRENGTH = "strength"
    OTHER = "other"


class Intensity(StrEnum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


class Level(StrEnum):
    BEGINNER = "BEGINNER"
    INTERMEDIATE = "INTERMEDIATE"
    ADVANCED = "ADVANCED"


class Day(StrEnum):
    MON = "MON"
    TUE = "TUE"
    WED = "WED"
    THU = "THU"
    FRI = "FRI"
    SAT = "SAT"
    SUN = "SUN"


class TimeSlot(StrEnum):
    MORNING = "MORNING"
    AFTERNOON = "AFTERNOON"
    EVENING = "EVENING"
    NIGHT = "NIGHT"


class PrimaryGoal(StrEnum):
    HEALTH = "HEALTH"
    PERFORMANCE = "PERFORMANCE"
    WEIGHT_MANAGEMENT = "WEIGHT_MANAGEMENT"
    RECREATION = "RECREATION"
    COMPETITION = "COMPETITION"


@dataclass(frozen=True)
class SportInput:
    """Un deporte con la forma JSON correcta (o `None` donde el transporte ya informó)."""

    sport: str | None = None
    frequency_per_week: int | None = None
    session_duration_minutes: int | None = None
    intensity: str | None = None
    level: str | None = None
    years_practicing: int | None = None


@dataclass(frozen=True)
class SportHabitsInput:
    """Valores con la forma JSON correcta; lo demás ya está en `transport_violations`.

    `sports`, `days` y `time_slots` son `None` si faltan o si el transporte los rechazó;
    un elemento `None` dentro de una lista es uno que el transporte ya informó.
    """

    expected_version: int | None = None
    sports: tuple[SportInput | None, ...] | None = None
    # `availability` llegó como objeto; sin él, `days` y `time_slots` no se evalúan.
    has_availability: bool = False
    days: tuple[str | None, ...] | None = None
    time_slots: tuple[str | None, ...] | None = None
    primary_goal: str | None = None
    transport_violations: tuple[Violation, ...] = ()

    def __repr__(self) -> str:
        # Datos personales: nunca en trazas ni logs.
        return f"SportHabitsInput(expected_version={self.expected_version})"


@dataclass(frozen=True)
class SportHabit:
    sport: Sport
    frequency_per_week: int
    session_duration_minutes: int
    intensity: Intensity
    level: Level
    years_practicing: int | None

    def document(self) -> dict[str, Any]:
        return {
            "sport": self.sport.value,
            "frequency_per_week": self.frequency_per_week,
            "session_duration_minutes": self.session_duration_minutes,
            "intensity": self.intensity.value,
            "level": self.level.value,
            "years_practicing": self.years_practicing,
        }


@dataclass(frozen=True)
class SportHabitsSection:
    expected_version: int | None
    sports: tuple[SportHabit, ...]
    days: tuple[Day, ...]
    time_slots: tuple[TimeSlot, ...]
    primary_goal: PrimaryGoal | None

    def __repr__(self) -> str:
        return f"SportHabitsSection(expected_version={self.expected_version})"

    def fields(self, now: datetime) -> dict[str, Any]:
        # Los hábitos no dependen del instante del comando.
        return self._values()

    def normalized(self) -> dict[str, Any]:
        return {"expected_version": self.expected_version, **self._values()}

    def _values(self) -> dict[str, Any]:
        return {
            "sports": [habit.document() for habit in self.sports],
            "availability": {
                "days": [day.value for day in self.days],
                "time_slots": [slot.value for slot in self.time_slots],
            },
            "primary_goal": self.primary_goal.value if self.primary_goal else None,
        }


def _canonical[EnumT: StrEnum](values: list[EnumT], catalog: type[EnumT]) -> tuple[EnumT, ...]:
    order = {member: position for position, member in enumerate(catalog)}
    return tuple(sorted(values, key=order.__getitem__))


_SPORT_REQUIRED = ("sport", "intensity", "level", "frequency_per_week", "session_duration_minutes")


def _sport(collector: Collector, item: SportInput, path: str) -> SportHabit | None:
    fields: dict[str, Any] = {}
    for name, catalog in (("sport", Sport), ("intensity", Intensity), ("level", Level)):
        value = getattr(item, name)
        if collector.required(value, f"{path}.{name}"):
            fields[name] = collector.catalog(value, f"{path}.{name}", catalog)
    for name, minimum, maximum in (
        ("frequency_per_week", FREQUENCY_MIN, FREQUENCY_MAX),
        ("session_duration_minutes", DURATION_MIN, DURATION_MAX),
    ):
        value = getattr(item, name)
        if collector.required(value, f"{path}.{name}"):
            fields[name] = collector.integer(value, f"{path}.{name}", minimum, maximum)
    years = collector.integer(
        item.years_practicing, f"{path}.years_practicing", YEARS_MIN, YEARS_MAX
    )
    if any(fields.get(name) is None for name in _SPORT_REQUIRED):
        return None
    if item.years_practicing is not None and years is None:
        return None
    return SportHabit(years_practicing=years, **fields)


def _sports(
    collector: Collector, sports: tuple[SportInput | None, ...] | None
) -> tuple[SportHabit, ...] | None:
    if sports is None:
        collector.required(None, "sports")
        return None
    if not sports:
        collector.add("sports", Issue.REQUIRED)
        return None
    if len(sports) > SPORTS_MAX:
        collector.add("sports", Issue.TOO_MANY_ITEMS)
        return None
    habits: list[SportHabit] = []
    seen: set[Sport] = set()
    complete = True
    for position, item in enumerate(sports):
        path = f"sports[{position}]"
        habit = _sport(collector, item, path) if item is not None else None
        if habit is None:
            complete = False
            continue
        if habit.sport in seen:
            collector.add(f"{path}.sport", Issue.DUPLICATE_VALUE)
            complete = False
            continue
        seen.add(habit.sport)
        habits.append(habit)
    if not complete:
        return None
    order = {member: position for position, member in enumerate(Sport)}
    return tuple(sorted(habits, key=lambda habit: order[habit.sport]))


def _choices[EnumT: StrEnum](
    collector: Collector,
    values: tuple[str | None, ...] | None,
    field: str,
    catalog: type[EnumT],
    maximum: int,
) -> tuple[EnumT, ...] | None:
    """Lista de 1 a `maximum` valores únicos del catálogo, en orden canónico."""
    if values is None:
        collector.required(None, field)
        return None
    if not values:
        collector.add(field, Issue.REQUIRED)
        return None
    if len(values) > maximum:
        collector.add(field, Issue.TOO_MANY_ITEMS)
        return None
    chosen: list[EnumT] = []
    complete = True
    for position, raw in enumerate(values):
        path = f"{field}[{position}]"
        value = collector.catalog(raw, path, catalog) if raw is not None else None
        if value is None:
            complete = False
        elif value in chosen:
            collector.add(path, Issue.DUPLICATE_VALUE)
            complete = False
        else:
            chosen.append(value)
    return _canonical(chosen, catalog) if complete else None


def validate_sport_habits(section: SportHabitsInput) -> SportHabitsSection:
    """Valida todo o nada y normaliza el orden de las listas."""
    collector = Collector(section.transport_violations)
    expected_version = collector.expected_version(section.expected_version)
    sports = _sports(collector, section.sports)
    if not section.has_availability:
        # Falta `availability` (o el transporte ya lo informó como no objeto).
        collector.required(None, "availability")
        days = time_slots = None
    else:
        days = _choices(collector, section.days, "availability.days", Day, DAYS_MAX)
        time_slots = _choices(
            collector, section.time_slots, "availability.time_slots", TimeSlot, TIME_SLOTS_MAX
        )
    primary_goal = collector.catalog(section.primary_goal, "primary_goal", PrimaryGoal)
    collector.raise_if_any()
    if sports is None or days is None or time_slots is None:
        raise InvalidProfileSection([])  # Inalcanzable: la falta ya se informó arriba.
    return SportHabitsSection(
        expected_version=expected_version,
        sports=sports,
        days=days,
        time_slots=time_slots,
        primary_goal=primary_goal,
    )
