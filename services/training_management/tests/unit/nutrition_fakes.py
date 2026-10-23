"""Dobles en memoria para probar HU014 sin infraestructura (datos sintéticos)."""

from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

from training_management.application.errors import (
    IdempotencyKeyConflict,
    NutritionRestrictionsUnavailable,
)
from training_management.application.ports import (
    IdempotencyRecord,
    NutritionPlanQuery,
    OutboxMessage,
)
from training_management.domain.authorizations.model import TrainingAuthorization
from training_management.domain.nutrition.catalog import FOOD_CATALOG, FoodCatalog
from training_management.domain.nutrition.model import NutritionPlan, NutritionRestrictions

AUTHOR = "nutrition.plan.author"
NO_HABITS = NutritionRestrictions(restrictions=(), allergies=(), diet_pattern=None, version=None)


def habits(
    *, restrictions: tuple[str, ...] = (), allergies: tuple[str, ...] = (), diet: str | None = None
) -> NutritionRestrictions:
    return NutritionRestrictions(
        restrictions=restrictions, allergies=allergies, diet_pattern=diet or "OMNIVORE", version=3
    )


@dataclass
class InMemoryNutritionStore:
    authorizations: dict[tuple[UUID, UUID], TrainingAuthorization] = field(default_factory=dict)
    plans: dict[UUID, NutritionPlan] = field(default_factory=dict)
    outbox: list[OutboxMessage] = field(default_factory=list)
    idempotency: dict[UUID, IdempotencyRecord] = field(default_factory=dict)
    authorization_checks: int = 0


class InMemoryNutritionUnitOfWork:
    """Aplica los cambios solo al confirmar y valida la misma unicidad que PostgreSQL."""

    def __init__(
        self,
        store: InMemoryNutritionStore,
        before_commit: Callable[[], None] | None = None,
        on_authorization_check: Callable[[int], None] | None = None,
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self.on_authorization_check = on_authorization_check
        self._plans: list[NutritionPlan] = []
        self._outbox: list[OutboxMessage] = []
        self._idempotency: list[IdempotencyRecord] = []
        self._deleted: list[UUID] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._plans, self._outbox, self._idempotency, self._deleted = [], [], [], []

    def find_idempotency(self, key: UUID) -> IdempotencyRecord | None:
        return self.store.idempotency.get(key)

    def delete_idempotency(self, key: UUID) -> None:
        self._deleted.append(key)

    def has_active_authorization(self, athlete_id: UUID, professional_id: UUID) -> bool:
        self.store.authorization_checks += 1
        if self.on_authorization_check is not None:
            self.on_authorization_check(self.store.authorization_checks)
        authorization = self.store.authorizations.get((athlete_id, professional_id))
        return authorization is not None and authorization.is_active

    def add_nutrition_plan(self, plan: NutritionPlan) -> None:
        self._plans.append(plan)

    def add_outbox(self, message: OutboxMessage) -> None:
        self._outbox.append(message)

    def add_idempotency(self, record: IdempotencyRecord) -> None:
        self._idempotency.append(record)

    def commit(self) -> None:
        if self.before_commit is not None:
            hook, self.before_commit = self.before_commit, None
            hook()
        for key in self._deleted:
            self.store.idempotency.pop(key, None)
        if any(record.key in self.store.idempotency for record in self._idempotency):
            raise IdempotencyKeyConflict()
        for plan in self._plans:
            self.store.plans[plan.plan_id] = plan
        self.store.outbox.extend(self._outbox)
        for record in self._idempotency:
            self.store.idempotency[record.key] = record


class FakeRestrictions:
    def __init__(self, result: NutritionRestrictions = NO_HABITS) -> None:
        self.result = result
        self.unavailable = False
        self.calls: list[UUID] = []

    def get(self, athlete_id: UUID) -> NutritionRestrictions:
        self.calls.append(athlete_id)
        if self.unavailable:
            raise NutritionRestrictionsUnavailable()
        return self.result


class StaticFoodCatalog:
    def __init__(self, catalog: FoodCatalog = FOOD_CATALOG) -> None:
        self.catalog = catalog
        self.loads = 0

    def load(self) -> FoodCatalog:
        self.loads += 1
        return self.catalog


class InMemoryNutritionPlanReader:
    def __init__(self, plans: dict[UUID, NutritionPlan]) -> None:
        self.plans = plans

    def get(self, plan_id: UUID) -> NutritionPlan | None:
        return self.plans.get(plan_id)

    def list(self, query: NutritionPlanQuery) -> list[NutritionPlan]:
        plans = [
            plan
            for plan in self.plans.values()
            if plan.athlete_id == query.athlete_id
            and (
                query.after is None
                or (plan.created_at, plan.plan_id) < (query.after.created_at, query.after.plan_id)
            )
        ]
        plans.sort(key=lambda plan: (plan.created_at, plan.plan_id), reverse=True)
        return plans[: query.limit]


def item(food_code: str = "OATS", quantity: str = "80", unit: str = "G") -> dict[str, Any]:
    return {"food_code": food_code, "quantity": quantity, "unit": unit}


def nutrition_body(athlete_id: UUID | None = None, **overrides: Any) -> dict[str, Any]:
    """Cuerpo válido de `POST /v1/nutrition-plans` (servicio) con datos sintéticos."""
    values: dict[str, Any] = {
        "athlete_id": str(athlete_id or uuid4()),
        "title": "Plan de recuperación",
        "objectives": ["Mejorar la recuperación", "Hidratación diaria"],
        "start_date": "2026-11-02",
        "end_date": "2026-11-29",
        "meals": [
            {"meal_type": "BREAKFAST", "items": [item("OATS", "80"), item("BANANA", "1", "UNIT")]},
            {"meal_type": "HYDRATION", "items": [item("WATER", "2000.0", "ML")]},
        ],
    }
    values.update(overrides)
    return values
