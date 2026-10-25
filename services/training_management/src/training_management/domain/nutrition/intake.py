"""Consumo de alimentos y bebidas de HU043 (ADR-037).

- El móvil genera `intake_id` (UUID v4) e informa `consumed_at` en UTC
  `AAAA-MM-DDTHH:MM:SSZ`; se rechaza más de 5 minutos por delante del reloj del backend o
  más de 30 días de antigüedad. `registered_at` lo asigna el backend.
- `meal_type` del catálogo de comidas (`BREAKFAST`, `LUNCH`, `DINNER`, `SNACK`,
  `HYDRATION`); `items` de 1 a 20 en el orden del deportista, cada uno con un `food_code`
  del catálogo vigente, `quantity` como **cadena** decimal con un decimal entre `0.1` y
  `5000.0` (se normaliza a un decimal: `"150"` → `"150.0"`) y una `unit` permitida para el
  alimento.
- El consumo queda en la versión 1 y es inmutable: editarlo o borrarlo queda fuera. Sin
  cálculo de calorías ni nutrientes y sin texto libre.

Las causas de `details[].issue` no las fija ADR-037; se reutilizan las estables del
servicio (ADR-030 y HU014) y quedan por ratificar (decisión de la tarea del 2026-10-25).
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from uuid import UUID

from training_management.domain.nutrition.catalog import FoodCatalog
from training_management.domain.nutrition.model import (
    MealType,
    format_quantity,
    normalize_quantity,
    quantity_in_range,
)

FUTURE_TOLERANCE = timedelta(minutes=5)
MAX_AGE = timedelta(days=30)
ITEMS_MIN = 1
ITEMS_MAX = 20
INTAKE_VERSION = 1
UUID_VERSION_4 = 4
REQUEST_FIELDS = ("intake_id", "consumed_at", "meal_type", "items")
ITEM_FIELDS = ("food_code", "quantity", "unit")
_UTC_SECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_DECIMAL_TEXT = re.compile(r"^[0-9]{1,4}(\.[0-9])?$")


class Issue(StrEnum):
    REQUIRED = "required"
    FIELD_NOT_ALLOWED = "field_not_allowed"
    INVALID_UUID = "invalid_uuid"
    INVALID_DATETIME = "invalid_datetime"
    TOO_FAR_IN_FUTURE = "too_far_in_future"
    TOO_OLD = "too_old"
    UNKNOWN_VALUE = "unknown_value"
    UNIT_NOT_ALLOWED = "unit_not_allowed"
    OUT_OF_RANGE = "out_of_range"
    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"


@dataclass(frozen=True)
class Violation:
    field: str | None
    issue: Issue


class InvalidFoodIntake(Exception):
    """La solicitud incumple ADR-037; informa todas las reglas incumplidas."""

    code = "validation_error"

    def __init__(self, violations: list[Violation]) -> None:
        super().__init__(", ".join(f"{item.field}: {item.issue}" for item in violations))
        self.violations = tuple(violations)


def utc_instant(value: datetime) -> str:
    """ISO 8601 en UTC con sufijo `Z`; la conversión a la zona del usuario es del cliente."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class IntakeItem:
    food_code: str
    quantity: Decimal
    unit: str


@dataclass(frozen=True)
class IntakeData:
    """Solicitud validada y normalizada; base de la idempotencia por `intake_id`."""

    intake_id: UUID
    consumed_at: datetime
    meal_type: MealType
    items: tuple[IntakeItem, ...]

    @property
    def fingerprint(self) -> str:
        """Huella de los datos del consumo (sin el identificador ni la clave)."""
        canonical = json.dumps(
            [
                utc_instant(self.consumed_at),
                self.meal_type.value,
                [
                    [item.food_code, format_quantity(item.quantity), item.unit]
                    for item in self.items
                ],
            ],
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FoodIntake:
    intake_id: UUID
    athlete_id: UUID
    consumed_at: datetime
    meal_type: MealType
    items: tuple[IntakeItem, ...]
    catalog_version: str
    registered_at: datetime
    version: int = INTAKE_VERSION

    @classmethod
    def register(
        cls, *, athlete_id: UUID, data: IntakeData, catalog: FoodCatalog, now: datetime
    ) -> "FoodIntake":
        return cls(
            intake_id=data.intake_id,
            athlete_id=athlete_id,
            consumed_at=data.consumed_at,
            meal_type=data.meal_type,
            items=data.items,
            catalog_version=catalog.catalog_version,
            registered_at=now.astimezone(UTC).replace(microsecond=0),
        )

    @property
    def data(self) -> IntakeData:
        return IntakeData(
            intake_id=self.intake_id,
            consumed_at=self.consumed_at,
            meal_type=self.meal_type,
            items=self.items,
        )

    def matches(self, athlete_id: UUID, data: IntakeData) -> bool:
        """Mismo deportista y mismos datos normalizados que ya se registraron."""
        return self.athlete_id == athlete_id and self.data.fingerprint == data.fingerprint


def _intake_id(value: object, violations: list[Violation]) -> UUID | None:
    if value is None:
        violations.append(Violation("intake_id", Issue.REQUIRED))
        return None
    try:
        parsed = UUID(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.version != UUID_VERSION_4:
        violations.append(Violation("intake_id", Issue.INVALID_UUID))
        return None
    return parsed


def _consumed_at(value: object, now: datetime, violations: list[Violation]) -> datetime | None:
    if value is None:
        violations.append(Violation("consumed_at", Issue.REQUIRED))
        return None
    if not isinstance(value, str) or not _UTC_SECONDS.match(value):
        violations.append(Violation("consumed_at", Issue.INVALID_DATETIME))
        return None
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        violations.append(Violation("consumed_at", Issue.INVALID_DATETIME))
        return None
    if instant > now + FUTURE_TOLERANCE:
        violations.append(Violation("consumed_at", Issue.TOO_FAR_IN_FUTURE))
        return None
    if instant < now - MAX_AGE:
        violations.append(Violation("consumed_at", Issue.TOO_OLD))
        return None
    return instant.astimezone(UTC)


def _meal_type(value: object, violations: list[Violation]) -> MealType | None:
    if value is None:
        violations.append(Violation("meal_type", Issue.REQUIRED))
        return None
    if not isinstance(value, str) or value not in {item.value for item in MealType}:
        violations.append(Violation("meal_type", Issue.UNKNOWN_VALUE))
        return None
    return MealType(value)


def parse_quantity(value: object) -> Decimal | None:
    """Cadena decimal con como máximo un decimal entre 0.1 y 5000.0; si no, `None`."""
    if not isinstance(value, str) or not _DECIMAL_TEXT.match(value):
        return None
    try:
        quantity = Decimal(value)
    except InvalidOperation:
        return None
    return normalize_quantity(quantity) if quantity_in_range(quantity) else None


def _item(
    index: int, item: object, catalog: FoodCatalog, violations: list[Violation]
) -> IntakeItem | None:
    path = f"items[{index}]"
    if not isinstance(item, dict):
        violations.append(Violation(path, Issue.REQUIRED))
        return None
    before = len(violations)
    violations.extend(
        Violation(f"{path}.{name}", Issue.FIELD_NOT_ALLOWED)
        for name in item
        if name not in ITEM_FIELDS
    )
    code = item.get("food_code")
    food = None
    if code is None:
        violations.append(Violation(f"{path}.food_code", Issue.REQUIRED))
    elif not isinstance(code, str) or (food := catalog.find(code)) is None:
        violations.append(Violation(f"{path}.food_code", Issue.UNKNOWN_VALUE))
    raw_quantity = item.get("quantity")
    quantity = parse_quantity(raw_quantity)
    if raw_quantity is None:
        violations.append(Violation(f"{path}.quantity", Issue.REQUIRED))
    elif quantity is None:
        # Un número JSON, una cadena no decimal o con más de un decimal es `out_of_range`.
        violations.append(Violation(f"{path}.quantity", Issue.OUT_OF_RANGE))
    unit = item.get("unit")
    if unit is None:
        violations.append(Violation(f"{path}.unit", Issue.REQUIRED))
    elif not isinstance(unit, str) or (food is not None and not food.allows(unit)):
        violations.append(Violation(f"{path}.unit", Issue.UNIT_NOT_ALLOWED))
    if food is None or quantity is None or len(violations) != before:
        return None
    return IntakeItem(food_code=food.food_code, quantity=quantity, unit=str(unit))


def _items(
    raw: object, catalog: FoodCatalog, violations: list[Violation]
) -> tuple[IntakeItem, ...]:
    if raw is None or not isinstance(raw, list):
        violations.append(Violation("items", Issue.REQUIRED))
        return ()
    if len(raw) < ITEMS_MIN:
        violations.append(Violation("items", Issue.TOO_SHORT))
        return ()
    if len(raw) > ITEMS_MAX:
        violations.append(Violation("items", Issue.TOO_LONG))
        return ()
    parsed = [_item(index, entry, catalog, violations) for index, entry in enumerate(raw)]
    return tuple(item for item in parsed if item is not None)


def validate_food_intake(
    body: object,
    catalog: FoodCatalog,
    now: datetime,
    transport_violations: tuple[Violation, ...] = (),
) -> IntakeData:
    """Solicitud normalizada o `InvalidFoodIntake` con todas las reglas incumplidas.

    `body` es el JSON recibido; `athlete_id` u otro campo no previsto es
    `field_not_allowed` porque la identidad sale del token.
    """
    violations = list(transport_violations)
    if not isinstance(body, dict):
        violations.append(Violation(None, Issue.REQUIRED))
        raise InvalidFoodIntake(violations)
    document: dict[str, Any] = body
    violations.extend(
        Violation(name, Issue.FIELD_NOT_ALLOWED) for name in document if name not in REQUEST_FIELDS
    )
    intake_id = _intake_id(document.get("intake_id"), violations)
    consumed_at = _consumed_at(document.get("consumed_at"), now, violations)
    meal_type = _meal_type(document.get("meal_type"), violations)
    items = _items(document.get("items"), catalog, violations)
    if violations or intake_id is None or consumed_at is None or meal_type is None:
        raise InvalidFoodIntake(violations)
    return IntakeData(
        intake_id=intake_id, consumed_at=consumed_at, meal_type=meal_type, items=items
    )
