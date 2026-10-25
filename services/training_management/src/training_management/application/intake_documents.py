"""HU043: documento del consumo de alimentos ↔ agregado.

El documento es el mismo en la respuesta de la API, el resultado idempotente guardado, el
payload de `consumo_alimento.registrado.v1` y la fila de `food_intake_view`. Un documento que
no cumple las reglas del agregado lanza `InvalidIntakeDocument` con un motivo sin datos del
consumo (el proyector lo usa como motivo de rechazo hacia la DLQ). No se valida contra el
catálogo: el productor ya lo hizo y la proyección no lee la base de escritura.
"""

import re
from datetime import datetime
from typing import Any
from uuid import UUID

from training_management.domain.nutrition.catalog import FoodUnit
from training_management.domain.nutrition.intake import (
    INTAKE_VERSION,
    ITEMS_MAX,
    ITEMS_MIN,
    FoodIntake,
    IntakeItem,
    parse_quantity,
)
from training_management.domain.nutrition.model import MealType

CATALOG_VERSION_MAX = 64
_FOOD_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_QUANTITY = re.compile(r"^[0-9]{1,4}\.[0-9]$")
_UNITS = frozenset(item.value for item in FoodUnit)
_MEAL_TYPES = frozenset(item.value for item in MealType)


class InvalidIntakeDocument(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _uuid(value: object, field: str) -> UUID:
    try:
        return UUID(str(value))
    except ValueError:
        raise InvalidIntakeDocument(f"{field}_not_uuid") from None


def _instant(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        raise InvalidIntakeDocument(f"{field}_not_datetime") from None
    if parsed.tzinfo is None:
        raise InvalidIntakeDocument(f"{field}_without_offset")
    return parsed


def _item(raw: object) -> IntakeItem:
    if not isinstance(raw, dict):
        raise InvalidIntakeDocument("item_invalid")
    food_code, quantity, unit = raw.get("food_code"), raw.get("quantity"), raw.get("unit")
    if not isinstance(food_code, str) or not _FOOD_CODE.match(food_code):
        raise InvalidIntakeDocument("food_code_invalid")
    parsed = parse_quantity(quantity)
    if not isinstance(quantity, str) or not _QUANTITY.match(quantity) or parsed is None:
        raise InvalidIntakeDocument("quantity_invalid")
    if unit not in _UNITS:
        raise InvalidIntakeDocument("unit_invalid")
    return IntakeItem(food_code=food_code, quantity=parsed, unit=str(unit))


def intake_from_document(document: dict[str, Any]) -> FoodIntake:
    meal_type = document.get("meal_type")
    if meal_type not in _MEAL_TYPES:
        raise InvalidIntakeDocument("meal_type_invalid")
    items = document.get("items")
    if not isinstance(items, list) or not ITEMS_MIN <= len(items) <= ITEMS_MAX:
        raise InvalidIntakeDocument("items_invalid")
    catalog_version = document.get("catalog_version")
    if not isinstance(catalog_version, str) or not 1 <= len(catalog_version) <= CATALOG_VERSION_MAX:
        raise InvalidIntakeDocument("catalog_version_invalid")
    version = document.get("version")
    if version != INTAKE_VERSION or isinstance(version, bool):
        raise InvalidIntakeDocument("version_invalid")
    return FoodIntake(
        intake_id=_uuid(document.get("intake_id"), "intake_id"),
        athlete_id=_uuid(document.get("athlete_id"), "athlete_id"),
        consumed_at=_instant(document.get("consumed_at"), "consumed_at"),
        meal_type=MealType(str(meal_type)),
        items=tuple(_item(item) for item in items),
        catalog_version=catalog_version,
        registered_at=_instant(document.get("registered_at"), "registered_at"),
        version=INTAKE_VERSION,
    )
