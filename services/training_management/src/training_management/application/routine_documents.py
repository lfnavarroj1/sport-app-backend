"""Reconstrucción validada de una rutina alimenticia desde su documento de estado completo
(HU030).

El mismo documento es la respuesta de la API, el resultado guardado para la idempotencia, el
payload de `plantilla_rutina.*.v1` y la fila de `nutrition_routine_template_view`. Un documento
que no cumple las reglas del agregado lanza `InvalidRoutineDocument` con un motivo sin datos de
la rutina (el proyector lo usa como motivo de rechazo hacia la DLQ). El catálogo de alimentos no
se consulta aquí: lo validó la publicación.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from training_management.domain.nutrition.model import MealType
from training_management.domain.plans.model import Sport
from training_management.domain.routines.model import (
    ALLERGENS_MAX,
    DESCRIPTION_MAX,
    DESCRIPTION_MIN,
    DURATION_DAYS_MAX,
    DURATION_DAYS_MIN,
    MEALS_MAX,
    MEALS_MIN,
    TITLE_MAX,
    TITLE_MIN,
    Allergy,
    DietPattern,
    NutritionRoutineTemplate,
    RoutineItem,
    RoutineMeal,
    RoutineStatus,
    meal_violations,
)
from training_management.domain.templates.model import Goal


class InvalidRoutineDocument(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _uuid(value: object, field: str) -> UUID:
    try:
        return UUID(str(value))
    except ValueError:
        raise InvalidRoutineDocument(f"{field}_not_uuid") from None


def _instant(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        raise InvalidRoutineDocument(f"{field}_not_datetime") from None
    if parsed.tzinfo is None:
        raise InvalidRoutineDocument(f"{field}_without_offset")
    return parsed


def _text(value: object, field: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise InvalidRoutineDocument(f"{field}_invalid")
    return value


def _int(value: object, field: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise InvalidRoutineDocument(f"{field}_invalid")
    return value


def _enum[E: StrEnum](kind: type[E], value: object, field: str) -> E:
    if value not in {item.value for item in kind}:
        raise InvalidRoutineDocument(f"{field}_invalid")
    return kind(str(value))


def _allergens(value: object) -> tuple[Allergy, ...]:
    if not isinstance(value, list) or len(value) > ALLERGENS_MAX:
        raise InvalidRoutineDocument("contains_allergens_invalid")
    allergens = tuple(_enum(Allergy, item, "contains_allergens") for item in value)
    # Sin repetir y en el orden del catálogo, como los deja la publicación.
    if allergens != tuple(item for item in Allergy if item in allergens):
        raise InvalidRoutineDocument("contains_allergens_invalid")
    return allergens


def _item(value: object) -> RoutineItem:
    if not isinstance(value, dict):
        raise InvalidRoutineDocument("daily_meals_invalid")
    food_code, portions = value.get("food_code"), value.get("portions")
    if (
        not isinstance(food_code, str)
        or not food_code
        or not isinstance(portions, int)
        or isinstance(portions, bool)
    ):
        raise InvalidRoutineDocument("daily_meals_invalid")
    return RoutineItem(food_code=food_code, portions=portions)


def _meal(index: int, value: object) -> RoutineMeal:
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise InvalidRoutineDocument("daily_meals_invalid")
    meal = RoutineMeal(
        meal=_enum(MealType, value.get("meal"), "meal"),
        items=tuple(_item(item) for item in value["items"]),
    )
    if meal_violations(index, meal):
        raise InvalidRoutineDocument("daily_meals_invalid")
    return meal


def routine_from_document(document: dict[str, Any]) -> NutritionRoutineTemplate:
    meals = document.get("daily_meals")
    if not isinstance(meals, list) or not MEALS_MIN <= len(meals) <= MEALS_MAX:
        raise InvalidRoutineDocument("daily_meals_invalid")
    version = document.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise InvalidRoutineDocument("version_invalid")
    status = _enum(RoutineStatus, document.get("status"), "status")
    retired_raw = document.get("retired_at")
    retired_at = _instant(retired_raw, "retired_at") if retired_raw is not None else None
    if (status is RoutineStatus.RETIRED) != (retired_at is not None):
        raise InvalidRoutineDocument("retired_at_mismatch")
    sport_raw = document.get("sport")
    return NutritionRoutineTemplate(
        template_id=_uuid(document.get("template_id"), "template_id"),
        professional_id=_uuid(document.get("professional_id"), "professional_id"),
        title=_text(document.get("title"), "title", TITLE_MIN, TITLE_MAX),
        description=_text(
            document.get("description"), "description", DESCRIPTION_MIN, DESCRIPTION_MAX
        ),
        goal=_enum(Goal, document.get("goal"), "goal"),
        sport=_enum(Sport, sport_raw, "sport") if sport_raw is not None else None,
        dietary_pattern=_enum(DietPattern, document.get("dietary_pattern"), "dietary_pattern"),
        contains_allergens=_allergens(document.get("contains_allergens")),
        duration_days=_int(
            document.get("duration_days"), "duration_days", DURATION_DAYS_MIN, DURATION_DAYS_MAX
        ),
        daily_meals=tuple(_meal(index, item) for index, item in enumerate(meals)),
        status=status,
        version=version,
        created_at=_instant(document.get("created_at"), "created_at"),
        updated_at=_instant(document.get("updated_at"), "updated_at"),
        published_at=_instant(document.get("published_at"), "published_at"),
        retired_at=retired_at,
    )
