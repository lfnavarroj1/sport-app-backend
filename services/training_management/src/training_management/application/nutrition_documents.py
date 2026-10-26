"""HU014: documento del plan de alimentación ↔ agregado.

El documento es el mismo en la respuesta de la API, el payload de
`plan.alimentacion_creado.v1`, la vista `nutrition_plan_view` y el resultado idempotente.
`plan_from_document` reconstruye un documento que este servicio escribió; los eventos
recibidos del tópico se validan antes en `application.messages`.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from training_management.domain.nutrition.events import nutrition_plan_document
from training_management.domain.nutrition.model import (
    Meal,
    MealType,
    NutritionPlan,
    NutritionPlanStatus,
    PlanItem,
)

WARNINGS_FIELD = "warnings"


def plan_from_document(document: dict[str, Any]) -> NutritionPlan:
    return NutritionPlan(
        plan_id=UUID(document["plan_id"]),
        athlete_id=UUID(document["athlete_id"]),
        professional_id=UUID(document["professional_id"]),
        title=document["title"],
        objectives=tuple(document["objectives"]),
        start_date=date.fromisoformat(document["start_date"]),
        end_date=date.fromisoformat(document["end_date"]),
        status=NutritionPlanStatus(document["status"]),
        version=int(document["version"]),
        catalog_version=document["catalog_version"],
        meals=tuple(
            Meal(
                meal_type=MealType(meal["meal_type"]),
                items=tuple(
                    PlanItem(
                        food_code=item["food_code"],
                        quantity=Decimal(item["quantity"]),
                        unit=item["unit"],
                    )
                    for item in meal["items"]
                ),
            )
            for meal in document["meals"]
        ),
        created_at=datetime.fromisoformat(document["created_at"]),
    )


def creation_document(plan: NutritionPlan, warnings: tuple[str, ...]) -> dict[str, Any]:
    """Respuesta del `POST`, guardada como resultado idempotente: plan y `warnings`."""
    return {**nutrition_plan_document(plan), WARNINGS_FIELD: list(warnings)}


def warnings_from_document(document: dict[str, Any]) -> tuple[str, ...]:
    return tuple(document.get(WARNINGS_FIELD, ()))
