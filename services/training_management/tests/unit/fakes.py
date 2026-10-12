"""Dobles en memoria para probar HU010 sin infraestructura."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

from training_management.application.errors import (
    AuthorizationConflict,
    CapabilitiesUnavailable,
    IdempotencyKeyConflict,
)
from training_management.application.ports import (
    DuplicateDelivery,
    IdempotencyRecord,
    OutboxMessage,
    PlanView,
)
from training_management.domain.authorizations.model import TrainingAuthorization
from training_management.domain.plans.model import TrainingPlan

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


class FixedClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


class FakePermissions:
    """Mismo permiso de autor que `actor-permissions.v1` para `service_provider`."""

    def __init__(self, grants: dict[str, set[str]] | None = None) -> None:
        self._grants = grants or {"service_provider": {"training.plan.author"}}

    def allows(self, actor_type: str, permission: str) -> bool:
        return permission in self._grants.get(actor_type, set())


class FakeCapabilities:
    def __init__(self, granted: dict[UUID, set[str]] | None = None) -> None:
        self.granted = granted or {}
        self.unavailable = False
        self.calls: list[tuple[UUID, str]] = []

    def has_capability(self, user_id: UUID, capability: str) -> bool:
        self.calls.append((user_id, capability))
        if self.unavailable:
            raise CapabilitiesUnavailable()
        return capability in self.granted.get(user_id, set())


@dataclass
class InMemoryTrainingStore:
    authorizations: dict[tuple[UUID, UUID], TrainingAuthorization] = field(default_factory=dict)
    plans: dict[UUID, TrainingPlan] = field(default_factory=dict)
    outbox: list[OutboxMessage] = field(default_factory=list)
    idempotency: dict[UUID, IdempotencyRecord] = field(default_factory=dict)


class InMemoryPlanningUnitOfWork:
    """Aplica los cambios solo al confirmar y valida la misma unicidad que PostgreSQL."""

    def __init__(
        self, store: InMemoryTrainingStore, before_commit: Callable[[], None] | None = None
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self._plans: list[TrainingPlan] = []
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
        authorization = self.store.authorizations.get((athlete_id, professional_id))
        return authorization is not None and authorization.is_active

    def add_plan(self, plan: TrainingPlan) -> None:
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


class InMemoryAuthorizationUnitOfWork:
    def __init__(
        self, store: InMemoryTrainingStore, before_commit: Callable[[], None] | None = None
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self._added: list[TrainingAuthorization] = []
        self._updated: list[TrainingAuthorization] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._added, self._updated = [], []

    def get_for_update(
        self, athlete_id: UUID, professional_id: UUID
    ) -> TrainingAuthorization | None:
        return self.store.authorizations.get((athlete_id, professional_id))

    def add(self, authorization: TrainingAuthorization) -> None:
        self._added.append(authorization)

    def update(self, authorization: TrainingAuthorization) -> None:
        self._updated.append(authorization)

    def commit(self) -> None:
        if self.before_commit is not None:
            hook, self.before_commit = self.before_commit, None
            hook()
        for authorization in self._added:
            key = (authorization.athlete_id, authorization.professional_id)
            if key in self.store.authorizations:
                raise AuthorizationConflict()
            self.store.authorizations[key] = authorization
        for authorization in self._updated:
            key = (authorization.athlete_id, authorization.professional_id)
            self.store.authorizations[key] = authorization


@dataclass
class InMemoryReadStore:
    views: dict[UUID, PlanView] = field(default_factory=dict)
    processed: dict[UUID, tuple[str, UUID]] = field(default_factory=dict)


class InMemoryPlanReader:
    def __init__(self, store: InMemoryReadStore) -> None:
        self.store = store

    def get(self, plan_id: UUID) -> PlanView | None:
        return self.store.views.get(plan_id)


class InMemoryProjectionUnitOfWork:
    def __init__(
        self, store: InMemoryReadStore, before_commit: Callable[[], None] | None = None
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self._views: list[PlanView] = []
        self._processed: list[tuple[UUID, str, UUID]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._views, self._processed = [], []

    def is_processed(self, event_id: UUID) -> bool:
        return event_id in self.store.processed

    def projected_version(self, plan_id: UUID) -> int | None:
        view = self.store.views.get(plan_id)
        return view.version if view is not None else None

    def add_plan_view(self, view: PlanView, projected_at: datetime) -> None:
        self._views.append(view)

    def add_processed(
        self, event_id: UUID, event_type: str, aggregate_id: UUID, processed_at: datetime
    ) -> None:
        self._processed.append((event_id, event_type, aggregate_id))

    def commit(self) -> None:
        if self.before_commit is not None:
            hook, self.before_commit = self.before_commit, None
            hook()
        if any(event_id in self.store.processed for event_id, _, _ in self._processed):
            raise DuplicateDelivery()
        for view in self._views:
            self.store.views[view.plan_id] = view
        for event_id, event_type, aggregate_id in self._processed:
            self.store.processed[event_id] = (event_type, aggregate_id)


def session(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "scheduled_date": date(2026, 11, 2),
        "sport": "running",
        "duration_minutes": 45,
        "intensity_rpe": 6,
    }
    values.update(overrides)
    return values


def plan_body(athlete_id: UUID | None = None, **overrides: Any) -> dict[str, Any]:
    """Cuerpo válido de `POST /v1/training-plans` (servicio) con datos sintéticos."""
    values: dict[str, Any] = {
        "athlete_id": str(athlete_id or uuid4()),
        "title": "Base aeróbica 10K",
        "objectives": ["Correr 10 km continuos", "Mejorar la cadencia"],
        "start_date": "2026-11-02",
        "end_date": "2026-11-29",
        "sessions": [
            {
                "scheduled_date": "2026-11-02",
                "sport": "running",
                "duration_minutes": 40,
                "intensity_rpe": 5,
            },
            {
                "scheduled_date": "2026-11-04",
                "sport": "strength",
                "duration_minutes": 30,
                "intensity_rpe": 6,
            },
        ],
    }
    values.update(overrides)
    return values
