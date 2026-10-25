"""Dobles en memoria para probar HU030 sin infraestructura (datos sintéticos)."""

from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from training_management.application.errors import IdempotencyKeyConflict
from training_management.application.ports import (
    CommandRecord,
    MyRoutinesQuery,
    OutboxMessage,
    RoutineSuggestionQuery,
)
from training_management.domain.nutrition.model import MealType
from training_management.domain.routines.model import (
    Allergy,
    DietPattern,
    NutritionRoutineTemplate,
    RoutineInput,
    RoutineItem,
    RoutineMeal,
    RoutineStatus,
)
from training_management.domain.routines.suggestions import is_compatible, suggestion_order
from training_management.domain.templates.model import Goal

AUTHOR = "nutrition.plan.author"
SUGGEST = "nutrition.routines.suggest"


def routine_body(**overrides: Any) -> dict[str, Any]:
    """Cuerpo válido de `POST /v1/nutrition-routine-templates` con datos sintéticos."""
    body: dict[str, Any] = {
        "title": "Desayunos de fondo",
        "description": "Rutina para semanas de carga aeróbica.",
        "goal": "PERFORMANCE",
        "sport": "running",
        "dietary_pattern": "VEGETARIAN",
        "contains_allergens": [],
        "duration_days": 14,
        "daily_meals": [
            {
                "meal": "BREAKFAST",
                "items": [
                    {"food_code": "OATS", "portions": 1},
                    {"food_code": "BANANA", "portions": 1},
                ],
            },
            {"meal": "LUNCH", "items": [{"food_code": "LENTILS", "portions": 2}]},
        ],
    }
    body.update(overrides)
    return body


def routine_input(**overrides: Any) -> RoutineInput:
    values: dict[str, Any] = {
        "title": "Desayunos de fondo",
        "description": "Rutina para semanas de carga aeróbica.",
        "goal": Goal.PERFORMANCE,
        "sport": None,
        "dietary_pattern": DietPattern.VEGETARIAN,
        "contains_allergens": (),
        "duration_days": 14,
        "daily_meals": (
            RoutineMeal(MealType.BREAKFAST, (RoutineItem("OATS", 1), RoutineItem("BANANA", 1))),
        ),
    }
    values.update(overrides)
    return RoutineInput(**values)


def meal(*codes: str, kind: MealType = MealType.LUNCH) -> tuple[RoutineMeal, ...]:
    return (RoutineMeal(kind, tuple(RoutineItem(code, 1) for code in codes)),)


def allergens(*values: str) -> tuple[Allergy, ...]:
    return tuple(Allergy(value) for value in values)


@dataclass
class InMemoryRoutineStore:
    routines: dict[UUID, NutritionRoutineTemplate] = field(default_factory=dict)
    outbox: list[OutboxMessage] = field(default_factory=list)
    commands: dict[tuple[UUID, str, UUID], CommandRecord] = field(default_factory=dict)


class InMemoryRoutineUnitOfWork:
    """Aplica los cambios solo al confirmar y valida la misma unicidad que PostgreSQL."""

    def __init__(
        self, store: InMemoryRoutineStore, before_commit: Callable[[], None] | None = None
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self._routines: list[NutritionRoutineTemplate] = []
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
        self._routines, self._outbox, self._commands, self._deleted = [], [], [], []

    def find_command(self, actor_id: UUID, operation: str, key: UUID) -> CommandRecord | None:
        return self.store.commands.get((actor_id, operation, key))

    def delete_command(self, actor_id: UUID, operation: str, key: UUID) -> None:
        self._deleted.append((actor_id, operation, key))

    def add_command(self, record: CommandRecord) -> None:
        self._commands.append(record)

    def get_routine_for_update(self, template_id: UUID) -> NutritionRoutineTemplate | None:
        return self.store.routines.get(template_id)

    def add_routine(self, routine: NutritionRoutineTemplate) -> None:
        self._routines.append(routine)

    def update_routine(self, routine: NutritionRoutineTemplate) -> None:
        self._routines.append(routine)

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
        for routine in self._routines:
            self.store.routines[routine.template_id] = routine
        self.store.outbox.extend(self._outbox)
        for record in self._commands:
            self.store.commands[(record.actor_id, record.operation, record.key)] = record


class InMemoryRoutineReader:
    """`nutrition_routine_template_view` en memoria con las mismas reglas que la consulta SQL."""

    def __init__(self, routines: dict[UUID, NutritionRoutineTemplate]) -> None:
        self.routines = routines
        self.suggest_queries: list[RoutineSuggestionQuery] = []

    def list_by_professional(self, query: MyRoutinesQuery) -> list[NutritionRoutineTemplate]:
        own = sorted(
            (r for r in self.routines.values() if r.professional_id == query.professional_id),
            key=lambda r: (r.created_at, r.template_id),
            reverse=True,
        )
        if query.after is not None:
            after = (query.after.created_at, query.after.template_id)
            own = [r for r in own if (r.created_at, r.template_id) < after]
        return own[: query.limit]

    def suggest(self, query: RoutineSuggestionQuery) -> list[NutritionRoutineTemplate]:
        self.suggest_queries.append(query)
        context = query.context
        published = [
            r
            for r in self.routines.values()
            if r.status is RoutineStatus.PUBLISHED
            and is_compatible(
                context,
                dietary_pattern=r.dietary_pattern,
                contains_allergens=r.contains_allergens,
            )
        ]
        published.sort(
            key=lambda r: suggestion_order(
                context,
                goal=r.goal,
                sport=r.sport,
                published_at=r.published_at,
                template_id=r.template_id,
            )
        )
        return published[: query.limit]
