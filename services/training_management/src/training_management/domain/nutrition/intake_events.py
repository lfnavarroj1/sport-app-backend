"""`consumo_alimento.registrado.v1` en `sportapp.entrenamientos.v1` (ADR-037, HU043).

- Agregado `food_intake`; `aggregate_id` y clave de orden: `intake_id`; versión 1 (el consumo
  es inmutable). `causation_id` = `Idempotency-Key` del comando.
- El payload es el estado completo del consumo (el mismo documento de la respuesta) para que
  `entrenamientos-read-projector` construya `food_intake_view`.
- Sin calorías, nutrientes, nombres, correos ni texto libre. Instantes en UTC con sufijo `Z`;
  cantidades como cadena con un decimal.
"""

from typing import Any

from training_management.domain.nutrition.intake import FoodIntake, utc_instant
from training_management.domain.nutrition.model import format_quantity

EVENT_TYPE = "consumo_alimento.registrado.v1"
SCHEMA_VERSION = 1
AGGREGATE_TYPE = "food_intake"


def food_intake_document(intake: FoodIntake) -> dict[str, Any]:
    """Estado completo del consumo: respuesta de la API, payload del evento y vista."""
    return {
        "intake_id": str(intake.intake_id),
        "athlete_id": str(intake.athlete_id),
        "consumed_at": utc_instant(intake.consumed_at),
        "meal_type": intake.meal_type.value,
        "items": [
            {
                "food_code": item.food_code,
                "quantity": format_quantity(item.quantity),
                "unit": item.unit,
            }
            for item in intake.items
        ],
        "catalog_version": intake.catalog_version,
        "version": intake.version,
        "registered_at": utc_instant(intake.registered_at),
    }


def food_intake_registered_payload(intake: FoodIntake) -> dict[str, Any]:
    return food_intake_document(intake)
