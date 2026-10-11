"""Puertos que la aplicación necesita; la infraestructura los implementa."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID

from affiliation_management.domain.subscriptions.model import PlanVersionRef, Subscription


class Clock(Protocol):
    def now(self) -> datetime: ...


class InboxResult(StrEnum):
    """Resultado registrado por cada evento consumido (trazabilidad de la deduplicación)."""

    SUBSCRIPTION_CREATED = "subscription_created"
    NOT_APPLICABLE = "not_applicable"
    ALREADY_ACTIVE = "already_active"


@dataclass(frozen=True)
class InboxRecord:
    event_id: UUID
    event_type: str
    user_id: UUID
    result: InboxResult
    processed_at: datetime


@dataclass(frozen=True)
class OutboxMessage:
    event_id: UUID
    event_type: str
    schema_version: int
    aggregate_type: str
    aggregate_id: UUID
    aggregate_version: int
    occurred_at: datetime
    correlation_id: UUID | None
    causation_id: str | None
    payload: dict[str, Any]


class DuplicateDelivery(Exception):
    """Otra entrega del mismo evento confirmó primero su registro en la inbox."""


class ActiveSubscriptionExists(Exception):
    """Otra transacción confirmó primero una suscripción activa para el usuario."""


class ProvisioningUnitOfWork(Protocol):
    """Transacción local del alta gratuita: inbox, suscripción y outbox juntas."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def inbox_contains(self, event_id: UUID) -> bool: ...

    def has_active_subscription(self, user_id: UUID) -> bool: ...

    def default_athlete_plan(self, now: datetime) -> PlanVersionRef | None:
        """Versión vigente del plan predeterminado para deportistas, si el catálogo existe."""
        ...

    def add_subscription(self, subscription: Subscription) -> None: ...

    def add_outbox(self, message: OutboxMessage) -> None: ...

    def add_inbox(self, record: InboxRecord) -> None: ...

    def commit(self) -> None:
        """Confirma todo o nada.

        Traduce las violaciones de unicidad a `DuplicateDelivery` (misma entrega
        registrada) o `ActiveSubscriptionExists` (suscripción activa concurrente).
        """
        ...


@dataclass(frozen=True)
class CapabilitySnapshot:
    plan: PlanVersionRef
    capabilities: tuple[str, ...]


class CapabilitiesReader(Protocol):
    def active_snapshot(self, user_id: UUID) -> CapabilitySnapshot | None: ...
