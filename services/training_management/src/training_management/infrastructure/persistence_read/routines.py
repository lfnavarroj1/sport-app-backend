"""Base de lectura de HU030: filas de `nutrition_routine_template_view`, rutinas del
profesional y sugerencias (la exclusión por alérgenos y patrón ocurre en esta consulta)."""

from datetime import datetime
from typing import Any

from sqlalchemy import Select, case, not_, select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from training_management.application.ports import MyRoutinesQuery, RoutineSuggestionQuery
from training_management.application.routine_documents import routine_from_document
from training_management.domain.routines.events import routine_document
from training_management.domain.routines.model import NutritionRoutineTemplate, RoutineStatus
from training_management.infrastructure.persistence_read.models import (
    NutritionRoutineTemplateViewRow,
)


def routine_view_values(
    routine: NutritionRoutineTemplate, projected_at: datetime
) -> dict[str, Any]:
    return {
        "template_id": routine.template_id,
        "professional_id": routine.professional_id,
        "status": routine.status.value,
        "goal": routine.goal.value,
        "sport": routine.sport.value if routine.sport is not None else None,
        "dietary_pattern": routine.dietary_pattern.value,
        "contains_allergens": [allergen.value for allergen in routine.contains_allergens],
        "document": routine_document(routine),
        "version": routine.version,
        "created_at": routine.created_at,
        "published_at": routine.published_at,
        "projected_at": projected_at,
    }


def suggestion_statement(query: RoutineSuggestionQuery) -> Select[tuple[dict[str, Any]]]:
    row = NutritionRoutineTemplateViewRow
    statement = select(row.document).where(row.status == RoutineStatus.PUBLISHED.value)
    context = query.context
    latest = (row.published_at.desc(), row.template_id.desc())
    if not context.personalized:
        # Sin hábitos alimenticios: las últimas publicadas, sin personalizar.
        return statement.order_by(*latest).limit(query.limit)
    # CA2: se excluye en el servicio, nunca en el cliente.
    statement = statement.where(
        row.dietary_pattern.in_(sorted(item.value for item in context.accepted_patterns))
    )
    if context.exclude_allergens:
        statement = statement.where(
            not_(row.contains_allergens.has_any([item.value for item in context.exclude_allergens]))
        )
    # Mismo orden que `domain.routines.suggestions.suggestion_order`.
    goal_value = context.primary_goal.value if context.primary_goal is not None else None
    goal_rank = case((row.goal == goal_value, 0), else_=1)
    sport_rank = case((row.sport.in_([sport.value for sport in context.sports]), 0), else_=1)
    return statement.order_by(goal_rank, sport_rank, *latest).limit(query.limit)


class SqlAlchemyRoutineReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_by_professional(self, query: MyRoutinesQuery) -> list[NutritionRoutineTemplate]:
        row = NutritionRoutineTemplateViewRow
        statement = select(row.document).where(row.professional_id == query.professional_id)
        if query.after is not None:
            # Keyset: estrictamente después de la última entregada en el orden descendente.
            statement = statement.where(
                tuple_(row.created_at, row.template_id)
                < tuple_(query.after.created_at, query.after.template_id)
            )
        statement = statement.order_by(row.created_at.desc(), row.template_id.desc()).limit(
            query.limit
        )
        with self._session_factory() as session:
            return [routine_from_document(item) for item in session.scalars(statement)]

    def suggest(self, query: RoutineSuggestionQuery) -> list[NutritionRoutineTemplate]:
        with self._session_factory() as session:
            return [
                routine_from_document(item) for item in session.scalars(suggestion_statement(query))
            ]
