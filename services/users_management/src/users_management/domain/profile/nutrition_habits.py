"""Sección de hábitos alimenticios del perfil (HU009, ADR-035).

Patrón alimentario, restricciones y alergias de catálogos cerrados (sin texto libre),
comidas por día e hidratación opcional. Alergias y restricciones son **datos de salud**:
exigen el consentimiento vigente y nunca aparecen en logs, trazas, auditoría ni errores.
El sistema no prescribe ni diagnostica.

Normalización canónica: las restricciones y las alergias se devuelven en el orden de su
catálogo, sea cual sea el orden recibido; así la misma información produce el mismo
documento y la misma huella idempotente. Los errores nombran la posición recibida
(`allergies[1]`).
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from users_management.domain.profile.physiological import validate_consent
from users_management.domain.profile.validation import (
    Collector,
    InvalidProfileSection,
    Issue,
    Violation,
    format_decimal,
    format_instant,
)

MEALS_PER_DAY_MIN, MEALS_PER_DAY_MAX = 1, 8
HYDRATION_MIN_LITERS = Decimal("0.5")
HYDRATION_MAX_LITERS = Decimal("10.0")


class DietPattern(StrEnum):
    OMNIVORE = "OMNIVORE"
    VEGETARIAN = "VEGETARIAN"
    VEGAN = "VEGAN"
    PESCATARIAN = "PESCATARIAN"
    FLEXITARIAN = "FLEXITARIAN"


class Restriction(StrEnum):
    GLUTEN_FREE = "GLUTEN_FREE"
    LACTOSE_FREE = "LACTOSE_FREE"
    LOW_SODIUM = "LOW_SODIUM"
    LOW_SUGAR = "LOW_SUGAR"
    KETO = "KETO"
    HALAL = "HALAL"
    KOSHER = "KOSHER"


class Allergy(StrEnum):
    PEANUTS = "PEANUTS"
    TREE_NUTS = "TREE_NUTS"
    MILK = "MILK"
    EGGS = "EGGS"
    WHEAT = "WHEAT"
    SOY = "SOY"
    FISH = "FISH"
    SHELLFISH = "SHELLFISH"
    SESAME = "SESAME"


# Cada lista admite cero elementos y, como mucho, todo su catálogo sin repetir.
RESTRICTIONS_MAX = len(Restriction)
ALLERGIES_MAX = len(Allergy)


@dataclass(frozen=True)
class NutritionHabitsInput:
    """Valores con la forma JSON correcta; lo demás ya está en `transport_violations`.

    `restrictions` y `allergies` son `None` si faltan o si el transporte los rechazó; un
    elemento `None` dentro de una lista es uno que el transporte ya informó.
    """

    expected_version: int | None = None
    consent_version: str | None = None
    diet_pattern: str | None = None
    restrictions: tuple[str | None, ...] | None = None
    allergies: tuple[str | None, ...] | None = None
    meals_per_day: int | None = None
    hydration_liters_per_day: str | None = None
    transport_violations: tuple[Violation, ...] = ()

    def __repr__(self) -> str:
        # Datos de salud: nunca en trazas ni logs.
        return f"NutritionHabitsInput(expected_version={self.expected_version})"


@dataclass(frozen=True)
class NutritionHabitsSection:
    expected_version: int | None
    consent_version: str
    diet_pattern: DietPattern
    restrictions: tuple[Restriction, ...]
    allergies: tuple[Allergy, ...]
    meals_per_day: int
    hydration_liters_per_day: Decimal | None

    def __repr__(self) -> str:
        return f"NutritionHabitsSection(expected_version={self.expected_version})"

    def fields(self, now: datetime) -> dict[str, Any]:
        """El consentimiento se acepta en el instante del comando."""
        return {
            "consent": {"version": self.consent_version, "accepted_at": format_instant(now)},
            **self._values(),
        }

    def normalized(self) -> dict[str, Any]:
        return {
            "expected_version": self.expected_version,
            "consent": {"version": self.consent_version},
            **self._values(),
        }

    def _values(self) -> dict[str, Any]:
        hydration = self.hydration_liters_per_day
        return {
            "diet_pattern": self.diet_pattern.value,
            "restrictions": [value.value for value in self.restrictions],
            "allergies": [value.value for value in self.allergies],
            "meals_per_day": self.meals_per_day,
            "hydration_liters_per_day": format_decimal(hydration)
            if hydration is not None
            else None,
        }


def _choices[EnumT: StrEnum](
    collector: Collector,
    values: tuple[str | None, ...] | None,
    field: str,
    catalog: type[EnumT],
) -> tuple[EnumT, ...] | None:
    """Lista obligatoria de 0 a `len(catalog)` valores únicos, en orden canónico."""
    if values is None:
        collector.required(None, field)
        return None
    if len(values) > len(catalog):
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
    if not complete:
        return None
    order = {member: position for position, member in enumerate(catalog)}
    return tuple(sorted(chosen, key=order.__getitem__))


def validate_nutrition_habits(
    section: NutritionHabitsInput, current_consent_version: str
) -> NutritionHabitsSection:
    """Valida todo o nada; sin consentimiento vigente no se acepta ningún valor."""
    collector = Collector(section.transport_violations)
    expected_version = collector.expected_version(section.expected_version)
    consent_version = validate_consent(collector, section.consent_version, current_consent_version)
    diet_pattern = None
    if collector.required(section.diet_pattern, "diet_pattern"):
        diet_pattern = collector.catalog(section.diet_pattern, "diet_pattern", DietPattern)
    restrictions = _choices(collector, section.restrictions, "restrictions", Restriction)
    allergies = _choices(collector, section.allergies, "allergies", Allergy)
    meals_per_day = None
    if collector.required(section.meals_per_day, "meals_per_day"):
        meals_per_day = collector.integer(
            section.meals_per_day, "meals_per_day", MEALS_PER_DAY_MIN, MEALS_PER_DAY_MAX
        )
    hydration = collector.decimal(
        section.hydration_liters_per_day,
        "hydration_liters_per_day",
        HYDRATION_MIN_LITERS,
        HYDRATION_MAX_LITERS,
    )
    collector.raise_if_any()
    if (
        consent_version is None
        or diet_pattern is None
        or restrictions is None
        or allergies is None
        or meals_per_day is None
    ):
        raise InvalidProfileSection([])  # Inalcanzable: la falta ya se informó arriba.
    return NutritionHabitsSection(
        expected_version=expected_version,
        consent_version=consent_version,
        diet_pattern=diet_pattern,
        restrictions=restrictions,
        allergies=allergies,
        meals_per_day=meals_per_day,
        hydration_liters_per_day=hydration,
    )
