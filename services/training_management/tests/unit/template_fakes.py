"""Dobles en memoria para probar HU028 sin infraestructura (datos sintéticos)."""

from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from training_management.application.errors import IdempotencyKeyConflict
from training_management.application.ports import (
    CommandRecord,
    MyTemplatesQuery,
    OutboxMessage,
    TemplateSuggestionQuery,
)
from training_management.domain.plans.model import Sport
from training_management.domain.templates.model import (
    DayOfWeek,
    Goal,
    Level,
    TemplateInput,
    TemplateStatus,
    TrainingPlanTemplate,
    WeeklySession,
)
from training_management.domain.templates.suggestions import personalized_order

AUTHOR = "training.plan.author"
SUGGEST = "training.plans.suggest"
OBJECTIVES = "Construir base aeróbica en ocho semanas."


def template_body(**overrides: Any) -> dict[str, Any]:
    """Cuerpo válido de `POST /v1/training-plan-templates` con datos sintéticos."""
    body: dict[str, Any] = {
        "title": "Base de 10 km",
        "objectives": OBJECTIVES,
        "sport": "running",
        "goal": "PERFORMANCE",
        "level": "BEGINNER",
        "duration_weeks": 8,
        "weekly_sessions": [
            {"day_of_week": "MON", "description": "Rodaje suave", "duration_minutes": 40},
            {"day_of_week": "THU", "description": "Series cortas", "duration_minutes": 45},
        ],
    }
    body.update(overrides)
    return body


def template_input(**overrides: Any) -> TemplateInput:
    values: dict[str, Any] = {
        "title": "Base de 10 km",
        "objectives": OBJECTIVES,
        "sport": Sport.RUNNING,
        "goal": Goal.PERFORMANCE,
        "level": Level.BEGINNER,
        "duration_weeks": 8,
        "weekly_sessions": (WeeklySession(DayOfWeek.MON, "Rodaje suave", 40),),
    }
    values.update(overrides)
    return TemplateInput(**values)


@dataclass
class InMemoryTemplateStore:
    templates: dict[UUID, TrainingPlanTemplate] = field(default_factory=dict)
    outbox: list[OutboxMessage] = field(default_factory=list)
    commands: dict[tuple[UUID, str, UUID], CommandRecord] = field(default_factory=dict)


class InMemoryTemplateUnitOfWork:
    """Aplica los cambios solo al confirmar y valida la misma unicidad que PostgreSQL."""

    def __init__(
        self, store: InMemoryTemplateStore, before_commit: Callable[[], None] | None = None
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self._templates: list[TrainingPlanTemplate] = []
        self._outbox: list[OutboxMessage] = []
        self._commands: list[CommandRecord] = []
        self._deleted: list[tuple[UUID, str, UUID]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._templates, self._outbox, self._commands, self._deleted = [], [], [], []

    def find_command(self, actor_id: UUID, operation: str, key: UUID) -> CommandRecord | None:
        return self.store.commands.get((actor_id, operation, key))

    def delete_command(self, actor_id: UUID, operation: str, key: UUID) -> None:
        self._deleted.append((actor_id, operation, key))

    def add_command(self, record: CommandRecord) -> None:
        self._commands.append(record)

    def get_template_for_update(self, template_id: UUID) -> TrainingPlanTemplate | None:
        return self.store.templates.get(template_id)

    def add_template(self, template: TrainingPlanTemplate) -> None:
        self._templates.append(template)

    def update_template(self, template: TrainingPlanTemplate) -> None:
        self._templates.append(template)

    def add_outbox(self, message: OutboxMessage) -> None:
        self._outbox.append(message)

    def commit(self) -> None:
        if self.before_commit is not None:
            hook, self.before_commit = self.before_commit, None
            hook()
        for key in self._deleted:
            self.store.commands.pop(key, None)
        for record in self._commands:
            if (record.actor_id, record.operation, record.key) in self.store.commands:
                raise IdempotencyKeyConflict()
        for template in self._templates:
            self.store.templates[template.template_id] = template
        self.store.outbox.extend(self._outbox)
        for record in self._commands:
            self.store.commands[(record.actor_id, record.operation, record.key)] = record


class InMemoryTemplateReader:
    """`training_plan_template_view` en memoria con el mismo orden que la consulta SQL."""

    def __init__(self, templates: dict[UUID, TrainingPlanTemplate]) -> None:
        self.templates = templates
        self.suggest_queries: list[TemplateSuggestionQuery] = []

    def list_by_professional(self, query: MyTemplatesQuery) -> list[TrainingPlanTemplate]:
        own = sorted(
            (t for t in self.templates.values() if t.professional_id == query.professional_id),
            key=lambda t: (t.created_at, t.template_id),
            reverse=True,
        )
        if query.after is not None:
            after = (query.after.created_at, query.after.template_id)
            own = [t for t in own if (t.created_at, t.template_id) < after]
        return own[: query.limit]

    def suggest(self, query: TemplateSuggestionQuery) -> list[TrainingPlanTemplate]:
        self.suggest_queries.append(query)
        published = [t for t in self.templates.values() if t.status is TemplateStatus.PUBLISHED]
        context = query.context
        if context.personalized:
            published = [t for t in published if t.sport in context.sports]
        published.sort(
            key=lambda t: personalized_order(
                context, goal=t.goal, published_at=t.published_at, template_id=t.template_id
            )
        )
        return published[: query.limit]
