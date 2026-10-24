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
    ActiveSessionView,
    AuthorizationView,
    DuplicateDelivery,
    HistoryQuery,
    IdempotencyRecord,
    OutboxMessage,
    PlanView,
    SessionHistoryEntry,
)
from training_management.domain.authorizations.model import TrainingAuthorization
from training_management.domain.metrics.model import TrainingMetricCalculation
from training_management.domain.nutrition.model import NutritionPlan
from training_management.domain.plans.model import TrainingPlan
from training_management.domain.routes.model import Route
from training_management.domain.templates.model import TrainingPlanTemplate

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
        self._outbox: list[OutboxMessage] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._added, self._updated, self._outbox = [], [], []

    def get_for_update(
        self, athlete_id: UUID, professional_id: UUID
    ) -> TrainingAuthorization | None:
        return self.store.authorizations.get((athlete_id, professional_id))

    def add(self, authorization: TrainingAuthorization) -> None:
        self._added.append(authorization)

    def update(self, authorization: TrainingAuthorization) -> None:
        self._updated.append(authorization)

    def add_outbox(self, message: OutboxMessage) -> None:
        self._outbox.append(message)

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
        self.store.outbox.extend(self._outbox)


@dataclass
class InMemoryReadStore:
    views: dict[UUID, PlanView] = field(default_factory=dict)
    processed: dict[UUID, tuple[str, UUID]] = field(default_factory=dict)
    authorizations: dict[tuple[UUID, UUID], AuthorizationView] = field(default_factory=dict)
    sessions: dict[UUID, SessionHistoryEntry] = field(default_factory=dict)
    active: dict[UUID, ActiveSessionView] = field(default_factory=dict)
    planned: dict[UUID, PlanView] = field(default_factory=dict)
    metric_results: dict[UUID, TrainingMetricCalculation] = field(default_factory=dict)
    nutrition_plans: dict[UUID, NutritionPlan] = field(default_factory=dict)
    routes: dict[UUID, Route] = field(default_factory=dict)
    templates: dict[UUID, TrainingPlanTemplate] = field(default_factory=dict)


class InMemoryHistoryReader:
    def __init__(self, store: InMemoryReadStore) -> None:
        self.store = store

    def list(self, query: HistoryQuery) -> list[SessionHistoryEntry]:
        entries = [
            entry
            for entry in self.store.sessions.values()
            if entry.athlete_id == query.athlete_id
            and (query.ended_from is None or entry.ended_at >= query.ended_from)
            and (query.ended_before is None or entry.ended_at < query.ended_before)
            and (query.sport is None or entry.sport == query.sport)
            and (
                query.after is None
                or (entry.ended_at, entry.session_id)
                < (query.after.ended_at, query.after.session_id)
            )
        ]
        entries.sort(key=lambda entry: (entry.ended_at, entry.session_id), reverse=True)
        return entries[: query.limit]


class InMemoryAuthorizationReader:
    def __init__(self, store: InMemoryReadStore) -> None:
        self.store = store

    def get(self, athlete_id: UUID, professional_id: UUID) -> AuthorizationView | None:
        return self.store.authorizations.get((athlete_id, professional_id))


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
        self._authorizations: list[AuthorizationView] = []
        self._sessions: list[SessionHistoryEntry] = []
        self._active: list[ActiveSessionView] = []
        self._removed: list[UUID] = []
        self._planned: list[PlanView] = []
        self._metrics: list[TrainingMetricCalculation] = []
        self._nutrition: list[NutritionPlan] = []
        self._routes: list[Route] = []
        self._templates: list[TrainingPlanTemplate] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._views, self._processed, self._authorizations, self._sessions = [], [], [], []
        self._active, self._removed, self._planned, self._metrics = [], [], [], []
        self._nutrition, self._routes, self._templates = [], [], []

    def is_processed(self, event_id: UUID) -> bool:
        return event_id in self.store.processed

    def projected_version(self, plan_id: UUID) -> int | None:
        view = self.store.views.get(plan_id)
        return view.version if view is not None else None

    def add_plan_view(self, view: PlanView, projected_at: datetime) -> None:
        self._views.append(view)

    def save_planned_sessions(self, view: PlanView, projected_at: datetime) -> None:
        self._planned.append(view)

    def authorization_version(self, athlete_id: UUID, professional_id: UUID) -> int | None:
        view = self.store.authorizations.get((athlete_id, professional_id))
        return view.version if view is not None else None

    def save_authorization_view(self, view: AuthorizationView, projected_at: datetime) -> None:
        self._authorizations.append(view)

    def session_version(self, session_id: UUID) -> int | None:
        entry = self.store.sessions.get(session_id)
        return entry.version if entry is not None else None

    def save_session_entry(self, entry: SessionHistoryEntry, projected_at: datetime) -> None:
        self._sessions.append(entry)

    def active_session_version(self, session_id: UUID) -> int | None:
        view = self.store.active.get(session_id)
        return view.version if view is not None else None

    def save_active_session(self, view: ActiveSessionView, projected_at: datetime) -> None:
        self._active.append(view)

    def delete_active_session(self, session_id: UUID) -> None:
        self._removed.append(session_id)

    def metric_result(self, calculation_id: UUID) -> TrainingMetricCalculation | None:
        return self.store.metric_results.get(calculation_id)

    def add_metric_result(
        self, calculation: TrainingMetricCalculation, projected_at: datetime
    ) -> None:
        self._metrics.append(calculation)

    def nutrition_plan_version(self, plan_id: UUID) -> int | None:
        plan = self.store.nutrition_plans.get(plan_id)
        return plan.version if plan is not None else None

    def save_nutrition_plan_view(self, plan: NutritionPlan, projected_at: datetime) -> None:
        self._nutrition.append(plan)

    def route_version(self, route_id: UUID) -> int | None:
        route = self.store.routes.get(route_id)
        return route.version if route is not None else None

    def save_route_view(self, route: Route, projected_at: datetime) -> None:
        self._routes.append(route)

    def template_version(self, template_id: UUID) -> int | None:
        template = self.store.templates.get(template_id)
        return template.version if template is not None else None

    def save_template_view(self, template: TrainingPlanTemplate, projected_at: datetime) -> None:
        self._templates.append(template)

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
        for authorization in self._authorizations:
            key = (authorization.athlete_id, authorization.professional_id)
            self.store.authorizations[key] = authorization
        for entry in self._sessions:
            self.store.sessions[entry.session_id] = entry
        for active in self._active:
            self.store.active[active.session_id] = active
        for planned in self._planned:
            self.store.planned[planned.plan_id] = planned
        for session_id in self._removed:
            self.store.active.pop(session_id, None)
        # Vistas de estado completo (HU034, HU014, HU026 y HU028).
        self.store.metric_results.update((c.calculation_id, c) for c in self._metrics)
        self.store.nutrition_plans.update((p.plan_id, p) for p in self._nutrition)
        self.store.routes.update((r.route_id, r) for r in self._routes)
        self.store.templates.update((t.template_id, t) for t in self._templates)
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
