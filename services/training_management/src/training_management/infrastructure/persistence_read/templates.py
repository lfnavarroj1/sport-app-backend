"""Base de lectura de HU028: filas de `training_plan_template_view`, plantillas del
profesional y sugerencias."""

from datetime import datetime
from typing import Any

from sqlalchemy import Select, case, select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from training_management.application.ports import MyTemplatesQuery, TemplateSuggestionQuery
from training_management.application.template_documents import template_from_document
from training_management.domain.templates.events import template_document
from training_management.domain.templates.model import TemplateStatus, TrainingPlanTemplate
from training_management.infrastructure.persistence_read.models import TrainingPlanTemplateViewRow


def template_view_values(template: TrainingPlanTemplate, projected_at: datetime) -> dict[str, Any]:
    return {
        "template_id": template.template_id,
        "professional_id": template.professional_id,
        "status": template.status.value,
        "sport": template.sport.value,
        "goal": template.goal.value,
        "document": template_document(template),
        "version": template.version,
        "created_at": template.created_at,
        "published_at": template.published_at,
        "projected_at": projected_at,
    }


def suggestion_statement(query: TemplateSuggestionQuery) -> Select[tuple[dict[str, Any]]]:
    row = TrainingPlanTemplateViewRow
    statement = select(row.document).where(row.status == TemplateStatus.PUBLISHED.value)
    context = query.context
    latest = (row.published_at.desc(), row.template_id.desc())
    if not context.personalized:
        # Sin deportes: las últimas publicadas, sin personalizar.
        return statement.order_by(*latest).limit(query.limit)
    statement = statement.where(row.sport.in_([sport.value for sport in context.sports]))
    if context.primary_goal is None:
        return statement.order_by(*latest).limit(query.limit)
    # Mismo orden que `domain.templates.suggestions.personalized_order`.
    goal_rank = case((row.goal == context.primary_goal.value, 0), else_=1)
    return statement.order_by(goal_rank, *latest).limit(query.limit)


class SqlAlchemyTemplateReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_by_professional(self, query: MyTemplatesQuery) -> list[TrainingPlanTemplate]:
        row = TrainingPlanTemplateViewRow
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
            return [template_from_document(item) for item in session.scalars(statement)]

    def suggest(self, query: TemplateSuggestionQuery) -> list[TrainingPlanTemplate]:
        with self._session_factory() as session:
            return [
                template_from_document(item)
                for item in session.scalars(suggestion_statement(query))
            ]
