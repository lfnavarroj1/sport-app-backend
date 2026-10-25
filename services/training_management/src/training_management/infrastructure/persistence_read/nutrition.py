"""Base de lectura de HU014 y HU043: filas de `nutrition_plan_view` y consultas del plan;
filas de `food_intake_view`."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from training_management.application.nutrition_documents import plan_from_document
from training_management.application.ports import NutritionPlanQuery
from training_management.domain.nutrition.events import nutrition_plan_document
from training_management.domain.nutrition.intake import FoodIntake
from training_management.domain.nutrition.intake_events import food_intake_document
from training_management.domain.nutrition.model import NutritionPlan
from training_management.infrastructure.persistence_read.models import NutritionPlanViewRow


def nutrition_plan_values(plan: NutritionPlan, projected_at: datetime) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "athlete_id": plan.athlete_id,
        "professional_id": plan.professional_id,
        "document": nutrition_plan_document(plan),
        "version": plan.version,
        "created_at": plan.created_at,
        "projected_at": projected_at,
    }


def food_intake_values(intake: FoodIntake, projected_at: datetime) -> dict[str, Any]:
    return {
        "intake_id": intake.intake_id,
        "athlete_id": intake.athlete_id,
        "consumed_at": intake.consumed_at,
        "meal_type": intake.meal_type.value,
        "items": food_intake_document(intake)["items"],
        "catalog_version": intake.catalog_version,
        "version": intake.version,
        "registered_at": intake.registered_at,
        "projected_at": projected_at,
    }


class SqlAlchemyNutritionPlanReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get(self, plan_id: UUID) -> NutritionPlan | None:
        with self._session_factory() as session:
            row = session.get(NutritionPlanViewRow, plan_id)
            return plan_from_document(row.document) if row is not None else None

    def list(self, query: NutritionPlanQuery) -> list[NutritionPlan]:
        row = NutritionPlanViewRow
        statement = select(row.document).where(row.athlete_id == query.athlete_id)
        if query.after is not None:
            # Keyset: estrictamente después del último entregado en el orden descendente.
            statement = statement.where(
                tuple_(row.created_at, row.plan_id)
                < tuple_(query.after.created_at, query.after.plan_id)
            )
        statement = statement.order_by(row.created_at.desc(), row.plan_id.desc()).limit(query.limit)
        with self._session_factory() as session:
            return [plan_from_document(document) for document in session.scalars(statement)]
