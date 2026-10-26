"""Agregado `NutritionRoutineTemplate` y reglas de HU030 (ADR-041).

Una rutina publicada no es un plan de alimentación asignado (HU014): no tiene deportista ni
fechas, y sugerirla no la asigna. Sin envío a domicilio, compra, pago ni cálculo calórico (CA5).

- `title` de 3 a 120 caracteres (espacios colapsados); `description` de 1 a 2.000.
- `goal` del catálogo de ADR-034; `sport` opcional del catálogo de HU010.
- `dietary_pattern` y `contains_allergens` con los catálogos de HU009 (ADR-035); los alérgenos
  se declaran sin repetir y se devuelven en el orden del catálogo.
- `duration_days` de 1 a 90 (la vigencia que ve el deportista).
- `daily_meals` de 1 a 8, en el orden del profesional, cada comida con `meal` (tipos de HU014) y
  de 1 a 15 ítems `{food_code, portions}`; `food_code` del catálogo vigente de alimentos
  (HU014/HU043) y `portions` entero de 1 a 10.
- Coherencia con el catálogo de alimentos (supuesto del 2026-10-25, por ratificar): un alimento
  cuyo `incompatible_with` contiene el patrón declarado se rechaza (`conflicts_with_restriction`,
  el motivo de HU014); los alérgenos del catálogo de cada alimento se agregan a
  `contains_allergens`, de modo que la exclusión del deportista nunca depende de una omisión.
- Nace `PUBLISHED` en versión 1; la única transición es `PUBLISHED` → `RETIRED`, que sube una vez
  la versión y conserva la fila.

La forma la valida primero el modelo de transporte; aquí se protegen las mismas reglas como
invariantes del agregado, cada una con su campo y motivo.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from training_management.domain.nutrition.catalog import FoodCatalog
from training_management.domain.nutrition.model import MealType
from training_management.domain.plans.errors import Violation
from training_management.domain.plans.model import Sport
from training_management.domain.templates.model import Goal

TITLE_MIN, TITLE_MAX = 3, 120
DESCRIPTION_MIN, DESCRIPTION_MAX = 1, 2000
DURATION_DAYS_MIN, DURATION_DAYS_MAX = 1, 90
MEALS_MIN, MEALS_MAX = 1, 8
ITEMS_MIN, ITEMS_MAX = 1, 15
PORTIONS_MIN, PORTIONS_MAX = 1, 10
INITIAL_VERSION = 1

# Motivos de `details[].issue` que agrega el dominio a los de la validación de forma.
OUT_OF_RANGE = "out_of_range"
INVALID_LENGTH = "invalid_length"
UNKNOWN_VALUE = "unknown_value"
DUPLICATE_VALUE = "duplicate_value"
CONFLICTS_WITH_RESTRICTION = "conflicts_with_restriction"


class DietPattern(StrEnum):
    """Catálogo `diet_pattern` de HU009 (ADR-035)."""

    OMNIVORE = "OMNIVORE"
    VEGETARIAN = "VEGETARIAN"
    VEGAN = "VEGAN"
    PESCATARIAN = "PESCATARIAN"
    FLEXITARIAN = "FLEXITARIAN"


class Allergy(StrEnum):
    """Catálogo de alergias de HU009 (ADR-035), en su orden."""

    PEANUTS = "PEANUTS"
    TREE_NUTS = "TREE_NUTS"
    MILK = "MILK"
    EGGS = "EGGS"
    WHEAT = "WHEAT"
    SOY = "SOY"
    FISH = "FISH"
    SHELLFISH = "SHELLFISH"
    SESAME = "SESAME"


ALLERGENS_MAX = len(Allergy)


class RoutineStatus(StrEnum):
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"


class InvalidRoutine(Exception):
    """CA4: la rutina incumple una regla; informa todas las reglas incumplidas."""

    code = "validation_error"

    def __init__(self, violations: list[Violation]) -> None:
        super().__init__(", ".join(f"{item.field}: {item.issue}" for item in violations))
        self.violations = tuple(violations)


class RoutineNotFound(Exception):
    """La rutina no existe o es de otro profesional; no se revela cuál."""

    code = "template_not_found"


class RoutineVersionConflict(Exception):
    """`expected_version` no coincide con la versión vigente de la rutina publicada."""

    code = "version_conflict"

    def __init__(self, current_version: int) -> None:
        super().__init__(str(current_version))
        self.current_version = current_version


@dataclass(frozen=True)
class RoutineItem:
    food_code: str
    portions: int


@dataclass(frozen=True)
class RoutineMeal:
    meal: MealType
    items: tuple[RoutineItem, ...]


@dataclass(frozen=True)
class RoutineInput:
    """Datos de la rutina con la forma JSON correcta."""

    title: str
    description: str
    goal: Goal
    sport: Sport | None
    dietary_pattern: DietPattern
    contains_allergens: tuple[Allergy, ...]
    duration_days: int
    daily_meals: tuple[RoutineMeal, ...]


@dataclass(frozen=True)
class NutritionRoutineTemplate:
    template_id: UUID
    professional_id: UUID
    title: str
    description: str
    goal: Goal
    sport: Sport | None
    dietary_pattern: DietPattern
    contains_allergens: tuple[Allergy, ...]
    duration_days: int
    daily_meals: tuple[RoutineMeal, ...]
    status: RoutineStatus
    version: int
    created_at: datetime
    updated_at: datetime
    published_at: datetime
    retired_at: datetime | None = None

    @classmethod
    def publish(
        cls,
        *,
        template_id: UUID,
        professional_id: UUID,
        data: RoutineInput,
        catalog: FoodCatalog,
        now: datetime,
    ) -> "NutritionRoutineTemplate":
        """Crea la rutina `PUBLISHED` en versión 1, o falla con todas las reglas incumplidas."""
        title = " ".join(data.title.split())
        description = data.description.strip()
        violations = _violations(title, description, data)
        violations.extend(_catalog_violations(data, catalog))
        if violations:
            raise InvalidRoutine(violations)
        published_at = now.astimezone(UTC)
        return cls(
            template_id=template_id,
            professional_id=professional_id,
            title=title,
            description=description,
            goal=data.goal,
            sport=data.sport,
            dietary_pattern=data.dietary_pattern,
            contains_allergens=_allergens(data, catalog),
            duration_days=data.duration_days,
            daily_meals=data.daily_meals,
            status=RoutineStatus.PUBLISHED,
            version=INITIAL_VERSION,
            created_at=published_at,
            updated_at=published_at,
            published_at=published_at,
        )

    def retire(self, now: datetime) -> "NutritionRoutineTemplate":
        """`PUBLISHED` → `RETIRED`: una sola subida de versión; la rutina se conserva."""
        if self.status is not RoutineStatus.PUBLISHED:
            raise ValueError("Solo una rutina publicada se puede retirar.")
        retired_at = now.astimezone(UTC)
        return replace(
            self,
            status=RoutineStatus.RETIRED,
            version=self.version + 1,
            updated_at=retired_at,
            retired_at=retired_at,
        )


def meal_violations(index: int, meal: RoutineMeal) -> list[Violation]:
    """Reglas de forma de una comida (sin catálogo); las comparte la reconstrucción."""
    prefix = f"daily_meals.{index}"
    violations: list[Violation] = []
    if not ITEMS_MIN <= len(meal.items) <= ITEMS_MAX:
        violations.append(Violation(f"{prefix}.items", OUT_OF_RANGE))
    for position, item in enumerate(meal.items):
        if not PORTIONS_MIN <= item.portions <= PORTIONS_MAX:
            violations.append(Violation(f"{prefix}.items.{position}.portions", OUT_OF_RANGE))
    return violations


def _violations(title: str, description: str, data: RoutineInput) -> list[Violation]:
    violations: list[Violation] = []
    if not TITLE_MIN <= len(title) <= TITLE_MAX:
        violations.append(Violation("title", INVALID_LENGTH))
    if not DESCRIPTION_MIN <= len(description) <= DESCRIPTION_MAX:
        violations.append(Violation("description", INVALID_LENGTH))
    if len(set(data.contains_allergens)) != len(data.contains_allergens):
        violations.append(Violation("contains_allergens", DUPLICATE_VALUE))
    if not DURATION_DAYS_MIN <= data.duration_days <= DURATION_DAYS_MAX:
        violations.append(Violation("duration_days", OUT_OF_RANGE))
    if not MEALS_MIN <= len(data.daily_meals) <= MEALS_MAX:
        violations.append(Violation("daily_meals", OUT_OF_RANGE))
    for index, meal in enumerate(data.daily_meals):
        violations.extend(meal_violations(index, meal))
    return violations


def _catalog_violations(data: RoutineInput, catalog: FoodCatalog) -> list[Violation]:
    """CA4: `food_code` del catálogo vigente y coherente con el patrón declarado."""
    violations: list[Violation] = []
    for meal_index, meal in enumerate(data.daily_meals):
        for item_index, item in enumerate(meal.items):
            field = f"daily_meals.{meal_index}.items.{item_index}.food_code"
            food = catalog.find(item.food_code)
            if food is None:
                violations.append(Violation(field, UNKNOWN_VALUE))
            elif data.dietary_pattern.value in food.incompatible_with:
                violations.append(Violation(field, CONFLICTS_WITH_RESTRICTION))
    return violations


def _allergens(data: RoutineInput, catalog: FoodCatalog) -> tuple[Allergy, ...]:
    declared: set[str] = {allergen.value for allergen in data.contains_allergens}
    for meal in data.daily_meals:
        for item in meal.items:
            food = catalog.find(item.food_code)
            if food is not None:
                declared.update(food.allergens)
    return tuple(item for item in Allergy if item.value in declared)
