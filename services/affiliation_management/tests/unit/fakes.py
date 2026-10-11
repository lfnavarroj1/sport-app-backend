"""Dobles en memoria para probar el alta gratuita sin infraestructura."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

from affiliation_management.application.commands.provision_free_subscription import (
    ProvisionFreeSubscription,
)
from affiliation_management.application.ports import (
    ActiveSubscriptionExists,
    CapabilitySnapshot,
    DuplicateDelivery,
    InboxRecord,
    OutboxMessage,
)
from affiliation_management.domain.catalog import CAPABILITIES_BY_PLAN
from affiliation_management.domain.subscriptions.model import PlanVersionRef, Subscription

FREE_V1 = PlanVersionRef(plan_code="FREE", version=1, catalog_version="plans-2026-10-11.v1")


@dataclass
class InMemoryAffiliationStore:
    subscriptions: dict[UUID, Subscription] = field(default_factory=dict)
    inbox: dict[UUID, InboxRecord] = field(default_factory=dict)
    outbox: list[OutboxMessage] = field(default_factory=list)
    default_plan: PlanVersionRef | None = FREE_V1

    def active_for(self, user_id: UUID) -> list[Subscription]:
        return [s for s in self.subscriptions.values() if s.user_id == user_id]


class InMemoryProvisioningUnitOfWork:
    """Aplica los cambios solo al confirmar y valida las mismas unicidades que PostgreSQL."""

    def __init__(
        self, store: InMemoryAffiliationStore, before_commit: Callable[[], None] | None = None
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self._subscriptions: list[Subscription] = []
        self._outbox: list[OutboxMessage] = []
        self._inbox: list[InboxRecord] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._subscriptions, self._outbox, self._inbox = [], [], []

    def inbox_contains(self, event_id: UUID) -> bool:
        return event_id in self.store.inbox

    def has_active_subscription(self, user_id: UUID) -> bool:
        return bool(self.store.active_for(user_id))

    def default_athlete_plan(self, now: datetime) -> PlanVersionRef | None:
        return self.store.default_plan

    def add_subscription(self, subscription: Subscription) -> None:
        self._subscriptions.append(subscription)

    def add_outbox(self, message: OutboxMessage) -> None:
        self._outbox.append(message)

    def add_inbox(self, record: InboxRecord) -> None:
        self._inbox.append(record)

    def commit(self) -> None:
        if self.before_commit is not None:
            hook, self.before_commit = self.before_commit, None
            hook()
        if any(record.event_id in self.store.inbox for record in self._inbox):
            raise DuplicateDelivery()
        if any(self.store.active_for(s.user_id) for s in self._subscriptions):
            raise ActiveSubscriptionExists()
        for subscription in self._subscriptions:
            self.store.subscriptions[subscription.subscription_id] = subscription
        self.store.outbox.extend(self._outbox)
        for record in self._inbox:
            self.store.inbox[record.event_id] = record


class FixedClock:
    def __init__(self, now: datetime | None = None) -> None:
        self.current = now or datetime(2026, 10, 11, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


class InMemoryCapabilitiesReader:
    def __init__(self, store: InMemoryAffiliationStore) -> None:
        self.store = store

    def active_snapshot(self, user_id: UUID) -> CapabilitySnapshot | None:
        active = self.store.active_for(user_id)
        if not active:
            return None
        plan = active[0].plan
        return CapabilitySnapshot(plan=plan, capabilities=CAPABILITIES_BY_PLAN[plan.plan_code])


def provision_with(
    store: InMemoryAffiliationStore,
    clock: FixedClock | None = None,
    unit_of_work: Callable[[], InMemoryProvisioningUnitOfWork] | None = None,
) -> ProvisionFreeSubscription:
    return ProvisionFreeSubscription(
        unit_of_work=unit_of_work or (lambda: InMemoryProvisioningUnitOfWork(store)),
        clock=clock or FixedClock(),
    )


def user_registered(**overrides: Any) -> dict[str, Any]:
    """Sobre de `usuario.registrado.v1` tal como lo publica Usuarios."""
    user_id = str(overrides.pop("user_id", uuid4()))
    payload = {
        "user_id": user_id,
        "full_name": "Ana Pérez",
        "email": "ana@example.com",
        "actor_type": overrides.pop("actor_type", "athlete"),
        "status": "active",
        "registered_at": "2026-10-11T12:00:00+00:00",
    }
    envelope: dict[str, Any] = {
        "event_id": str(uuid4()),
        "event_type": "usuario.registrado.v1",
        "occurred_at": "2026-10-11T12:00:00+00:00",
        "producer": "gestion-usuarios",
        "aggregate_id": user_id,
        "aggregate_version": 1,
        "correlation_id": str(uuid4()),
        "causation_id": str(uuid4()),
        "schema_version": 1,
        "payload": payload,
    }
    envelope.update(overrides)
    return envelope
