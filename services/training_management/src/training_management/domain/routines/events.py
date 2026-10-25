"""`plantilla_rutina.publicada.v1` y `.retirada.v1` en `sportapp.entrenamientos.v1`
(ADR-041, HU030).

- Agregado `nutrition_routine_template`; `aggregate_id` y clave de orden: `template_id`; versión
  1 al publicar y la siguiente al retirar.
- `causation_id`: la `Idempotency-Key` al publicar; al retirar, el `template_id` (el retiro no
  lleva clave: lo protege `expected_version`).
- El payload es el estado completo de la rutina (el mismo documento de la respuesta), para que
  `entrenamientos-read-projector` construya `nutrition_routine_template_view` aplicando solo
  versiones mayores. Sin datos de deportistas (ni sus alergias); instantes en UTC con `Z`.
"""

from typing import Any

from training_management.domain.routines.model import NutritionRoutineTemplate, RoutineStatus
from training_management.domain.templates.events import utc_instant

PUBLISHED_EVENT_TYPE = "plantilla_rutina.publicada.v1"
RETIRED_EVENT_TYPE = "plantilla_rutina.retirada.v1"
SCHEMA_VERSION = 1
AGGREGATE_TYPE = "nutrition_routine_template"
EVENT_TYPE_BY_STATUS = {
    RoutineStatus.PUBLISHED: PUBLISHED_EVENT_TYPE,
    RoutineStatus.RETIRED: RETIRED_EVENT_TYPE,
}


def routine_document(routine: NutritionRoutineTemplate) -> dict[str, Any]:
    """Estado completo: respuesta de la API, payload del evento y vista de lectura."""
    return {
        "template_id": str(routine.template_id),
        "professional_id": str(routine.professional_id),
        "title": routine.title,
        "description": routine.description,
        "goal": routine.goal.value,
        "sport": routine.sport.value if routine.sport is not None else None,
        "dietary_pattern": routine.dietary_pattern.value,
        "contains_allergens": [allergen.value for allergen in routine.contains_allergens],
        "duration_days": routine.duration_days,
        "daily_meals": [
            {
                "meal": meal.meal.value,
                "items": [
                    {"food_code": item.food_code, "portions": item.portions} for item in meal.items
                ],
            }
            for meal in routine.daily_meals
        ],
        "status": routine.status.value,
        "version": routine.version,
        "created_at": utc_instant(routine.created_at),
        "updated_at": utc_instant(routine.updated_at),
        "published_at": utc_instant(routine.published_at),
        "retired_at": utc_instant(routine.retired_at) if routine.retired_at is not None else None,
    }
