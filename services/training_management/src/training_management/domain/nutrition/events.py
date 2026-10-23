"""`plan.alimentacion_creado.v1` en `sportapp.entrenamientos.v1` (ADR-037, HU014).

- Agregado `nutrition_plan`; `aggregate_id` y clave de orden: `plan_id`; versión 1.
- `causation_id` = `Idempotency-Key` del comando.
- El payload es el estado completo del plan (el mismo documento de la respuesta, sin
  `warnings`) para que `entrenamientos-read-projector` construya `nutrition_plan_view`.
- No transporta alergias, restricciones, nombres, correos ni texto libre clínico.
- Instantes en UTC con sufijo `Z`; cantidades como cadena con un decimal.
"""

from datetime import UTC, datetime
from typing import Any

from training_management.domain.nutrition.model import NutritionPlan, format_quantity

EVENT_TYPE = "plan.alimentacion_creado.v1"
SCHEMA_VERSION = 1
AGGREGATE_TYPE = "nutrition_plan"


def utc_instant(value: datetime) -> str:
    """ISO 8601 en UTC con sufijo `Z`; la conversión a la zona del usuario es del cliente."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def nutrition_plan_document(plan: NutritionPlan) -> dict[str, Any]:
    """Estado completo del plan: respuesta de la API, payload del evento y vista de lectura."""
    return {
        "plan_id": str(plan.plan_id),
        "athlete_id": str(plan.athlete_id),
        "professional_id": str(plan.professional_id),
        "title": plan.title,
        "objectives": list(plan.objectives),
        "start_date": plan.start_date.isoformat(),
        "end_date": plan.end_date.isoformat(),
        "status": plan.status.value,
        "version": plan.version,
        "catalog_version": plan.catalog_version,
        "meals": [
            {
                "meal_type": meal.meal_type.value,
                "items": [
                    {
                        "food_code": item.food_code,
                        "quantity": format_quantity(item.quantity),
                        "unit": item.unit,
                    }
                    for item in meal.items
                ],
            }
            for meal in plan.meals
        ],
        "created_at": utc_instant(plan.created_at),
    }


def nutrition_plan_created_payload(plan: NutritionPlan) -> dict[str, Any]:
    return nutrition_plan_document(plan)
