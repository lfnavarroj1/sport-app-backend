"""Agregado `NutritionPlan` y reglas de HU014 (ADR-037; contrato fijado el 2026-10-22).

- Título de 1 a 120 caracteres; de 1 a 5 objetivos de 1 a 200 caracteres.
- `start_date` no posterior a `end_date`; periodo de hasta 26 semanas (182 días inclusivos).
- `meals` de 1 a 8: el menú diario del periodo, en el orden del profesional, cada comida con
  `meal_type` y de 1 a 15 ítems `{food_code, quantity, unit}`.
- `food_code` del catálogo vigente; `unit` entre las permitidas del alimento; `quantity`
  decimal con un decimal entre 0.1 y 5000.0, normalizada a un decimal (`150` → `150.0`).
- Bloqueos (ADR-037, decisión «bloquear, no advertir»): un alimento cuyos `allergens`
  intersecten las alergias del deportista, o cuyo `incompatible_with` intersecte su
  `diet_pattern` o sus `restrictions`, impide crear el plan; un detalle por ítem y causa.
- El plan nace `ASSIGNED` en versión 1 y es inmutable: editarlo o versionarlo es otra historia.

La forma (longitudes, cantidades de elementos y enumeraciones) la valida primero el modelo de
transporte (`validation_error`); aquí se protegen como invariantes del agregado.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from uuid import UUID

from training_management.domain.nutrition.catalog import FoodCatalog
from training_management.domain.plans.errors import Violation

TITLE_MAX_LENGTH = 120
OBJECTIVES_MAX = 5
OBJECTIVE_MAX_LENGTH = 200
MAX_PERIOD = timedelta(weeks=26)
MEALS_MAX = 8
ITEMS_PER_MEAL_MAX = 15
QUANTITY_MIN = Decimal("0.1")
QUANTITY_MAX = Decimal("5000.0")
QUANTITY_QUANTUM = Decimal("0.1")
PLAN_VERSION = 1

# Motivos estables de `details[].issue` en `nutrition_plan_invalid` (contrato del 2026-10-22).
# Uno nuevo es un cambio de contrato y debe agregarse aquí y en `bff-web`.
END_DATE_BEFORE_START_DATE = "end_date_before_start_date"
PERIOD_TOO_LONG = "period_too_long"
UNKNOWN_VALUE = "unknown_value"
UNIT_NOT_ALLOWED = "unit_not_allowed"
OUT_OF_RANGE = "out_of_range"
CONFLICTS_WITH_ALLERGY = "conflicts_with_allergy"
CONFLICTS_WITH_RESTRICTION = "conflicts_with_restriction"
NUTRITION_PLAN_ISSUES = (
    END_DATE_BEFORE_START_DATE,
    PERIOD_TOO_LONG,
    UNKNOWN_VALUE,
    UNIT_NOT_ALLOWED,
    OUT_OF_RANGE,
    CONFLICTS_WITH_ALLERGY,
    CONFLICTS_WITH_RESTRICTION,
)


class MealType(StrEnum):
    BREAKFAST = "BREAKFAST"
    LUNCH = "LUNCH"
    DINNER = "DINNER"
    SNACK = "SNACK"
    HYDRATION = "HYDRATION"


class NutritionPlanStatus(StrEnum):
    ASSIGNED = "ASSIGNED"


class InvalidNutritionPlan(Exception):
    """El plan incumple reglas o choca con alergias o restricciones del deportista."""

    code = "nutrition_plan_invalid"

    def __init__(self, violations: list[Violation]) -> None:
        super().__init__(", ".join(f"{item.field}: {item.issue}" for item in violations))
        self.violations = tuple(violations)

    @property
    def blocked(self) -> bool:
        """`True` si el rechazo se debe a alergias o restricciones (no a reglas)."""
        return any(
            item.issue in (CONFLICTS_WITH_ALLERGY, CONFLICTS_WITH_RESTRICTION)
            for item in self.violations
        )


@dataclass(frozen=True)
class PlanItem:
    food_code: str
    quantity: Decimal
    unit: str


@dataclass(frozen=True)
class Meal:
    meal_type: MealType
    items: tuple[PlanItem, ...]


@dataclass(frozen=True)
class NutritionRestrictions:
    """Restricciones del deportista según Usuarios (ADR-035). Nunca se persisten ni se
    registran: solo deciden si el plan se bloquea. `version` nula = sin hábitos registrados."""

    restrictions: tuple[str, ...]
    allergies: tuple[str, ...]
    diet_pattern: str | None
    version: int | None

    @property
    def habits_missing(self) -> bool:
        return self.version is None


@dataclass(frozen=True)
class NutritionPlan:
    plan_id: UUID
    athlete_id: UUID
    professional_id: UUID
    title: str
    objectives: tuple[str, ...]
    start_date: date
    end_date: date
    status: NutritionPlanStatus
    version: int
    catalog_version: str
    meals: tuple[Meal, ...]
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        plan_id: UUID,
        athlete_id: UUID,
        professional_id: UUID,
        title: str,
        objectives: list[str],
        start_date: date,
        end_date: date,
        meals: list[Meal],
        catalog: FoodCatalog,
        now: datetime,
    ) -> "NutritionPlan":
        """Crea el plan asignado en versión 1, o falla con todas las reglas incumplidas."""
        normalized_title = " ".join(title.split())
        normalized_objectives = tuple(" ".join(item.split()) for item in objectives)
        _require_shape(normalized_title, normalized_objectives, meals)
        violations = [
            *_period_violations(start_date, end_date),
            *_item_violations(meals, catalog),
        ]
        if violations:
            raise InvalidNutritionPlan(violations)
        return cls(
            plan_id=plan_id,
            athlete_id=athlete_id,
            professional_id=professional_id,
            title=normalized_title,
            objectives=normalized_objectives,
            start_date=start_date,
            end_date=end_date,
            status=NutritionPlanStatus.ASSIGNED,
            version=PLAN_VERSION,
            catalog_version=catalog.catalog_version,
            meals=tuple(
                Meal(
                    meal_type=meal.meal_type,
                    items=tuple(
                        PlanItem(item.food_code, normalize_quantity(item.quantity), item.unit)
                        for item in meal.items
                    ),
                )
                for meal in meals
            ),
            created_at=now,
        )

    def conflicts(
        self, catalog: FoodCatalog, restrictions: NutritionRestrictions
    ) -> list[Violation]:
        """Bloqueos por alergia y por restricción, un detalle por ítem y causa."""
        allergies = set(restrictions.allergies)
        blocking = set(restrictions.restrictions)
        if restrictions.diet_pattern is not None:
            blocking.add(restrictions.diet_pattern)
        violations: list[Violation] = []
        for meal_index, meal in enumerate(self.meals):
            for item_index, item in enumerate(meal.items):
                food = catalog.find(item.food_code)
                if food is None:
                    continue
                field = f"meals.{meal_index}.items.{item_index}.food_code"
                if allergies.intersection(food.allergens):
                    violations.append(Violation(field, CONFLICTS_WITH_ALLERGY))
                if blocking.intersection(food.incompatible_with):
                    violations.append(Violation(field, CONFLICTS_WITH_RESTRICTION))
        return violations


def quantity_in_range(quantity: Decimal) -> bool:
    """Decimal finito con como máximo un decimal, entre 0.1 y 5000.0."""
    try:
        exponent = quantity.as_tuple().exponent
    except (InvalidOperation, ValueError):
        return False
    if not isinstance(exponent, int) or not quantity.is_finite():
        return False
    return exponent >= -1 and QUANTITY_MIN <= quantity <= QUANTITY_MAX


def normalize_quantity(quantity: Decimal) -> Decimal:
    return quantity.quantize(QUANTITY_QUANTUM)


def format_quantity(quantity: Decimal) -> str:
    """Cadena con exactamente un decimal (`150` → `"150.0"`)."""
    return format(normalize_quantity(quantity), "f")


def _require_shape(title: str, objectives: tuple[str, ...], meals: list[Meal]) -> None:
    shape_ok = (
        1 <= len(title) <= TITLE_MAX_LENGTH
        and 1 <= len(objectives) <= OBJECTIVES_MAX
        and all(1 <= len(item) <= OBJECTIVE_MAX_LENGTH for item in objectives)
        and 1 <= len(meals) <= MEALS_MAX
        and all(1 <= len(meal.items) <= ITEMS_PER_MEAL_MAX for meal in meals)
    )
    if not shape_ok:
        raise ValueError("La forma del plan de alimentación no cumple el contrato.")


def _period_violations(start_date: date, end_date: date) -> list[Violation]:
    if end_date < start_date:
        return [Violation("end_date", END_DATE_BEFORE_START_DATE)]
    # Periodo inclusivo: 26 semanas son 182 días contados desde `start_date`.
    if end_date - start_date >= MAX_PERIOD:
        return [Violation("end_date", PERIOD_TOO_LONG)]
    return []


def _item_violations(meals: list[Meal], catalog: FoodCatalog) -> list[Violation]:
    violations: list[Violation] = []
    for meal_index, meal in enumerate(meals):
        for item_index, item in enumerate(meal.items):
            prefix = f"meals.{meal_index}.items.{item_index}"
            food = catalog.find(item.food_code)
            if food is None:
                violations.append(Violation(f"{prefix}.food_code", UNKNOWN_VALUE))
            elif not food.allows(item.unit):
                violations.append(Violation(f"{prefix}.unit", UNIT_NOT_ALLOWED))
            if not quantity_in_range(item.quantity):
                violations.append(Violation(f"{prefix}.quantity", OUT_OF_RANGE))
    return violations
