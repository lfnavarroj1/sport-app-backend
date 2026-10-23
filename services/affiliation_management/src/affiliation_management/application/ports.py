"""Puertos que la aplicación necesita; la infraestructura los implementa."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID

from affiliation_management.domain.subscriptions.change import (
    BillingCommitment,
    CatalogPlan,
    Movement,
)
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

    def none_offer_id(self, plan: PlanVersionRef) -> UUID | None:
        """Oferta `NONE` de la versión del plan gratuito (HU016), si el catálogo la tiene."""
        ...

    def add_subscription(self, subscription: Subscription) -> None: ...

    def add_movement(self, movement: Movement) -> None:
        """HU016: el alta registra `FREE_ACTIVATED` en `subscription_history`."""
        ...

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


# --- HU016 (ADR-036): catálogo vigente y cambio de suscripción ---


@dataclass(frozen=True)
class Caller:
    user_id: UUID
    actor_type: str


@dataclass(frozen=True)
class IdempotencyRecord:
    actor_id: UUID
    operation: str
    key: UUID
    request_fingerprint: str
    resource_id: UUID
    response: dict[str, Any]
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class OfferView:
    periodicity: str
    price: Decimal
    currency: str
    duration_months: int | None


@dataclass(frozen=True)
class PlanView:
    plan_code: str
    name: str
    description: str
    level: int
    version: int
    selectable: bool
    capabilities: tuple[str, ...]
    offers: tuple[OfferView, ...]


@dataclass(frozen=True)
class SubscriptionView:
    """Suscripción vigente con el precio de su oferta (`None` si la versión no tiene ofertas)."""

    subscription: Subscription
    price: Decimal | None


class CatalogReader(Protocol):
    def current_plan(self, plan_code: str, now: datetime) -> CatalogPlan | None:
        """Última versión vigente del plan con sus ofertas vigentes."""
        ...

    def catalog(self, catalog_version: str) -> list[PlanView]:
        """Planes de una versión del catálogo, en el orden de presentación."""
        ...


class SubscriptionReader(Protocol):
    def active(self, user_id: UUID) -> SubscriptionView | None: ...


class SubscriptionChangeUnitOfWork(Protocol):
    """Transacción local del cambio: cierre, alta, compromisos, historial, outbox y clave."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def find_idempotency(
        self, actor_id: UUID, operation: str, key: UUID
    ) -> IdempotencyRecord | None: ...

    def delete_idempotency(self, actor_id: UUID, operation: str, key: UUID) -> None: ...

    def lock_active_subscription(self, user_id: UUID) -> Subscription | None:
        """Suscripción `ACTIVE` del usuario bloqueada (`FOR UPDATE`) hasta el COMMIT."""
        ...

    def active_subscription(self, user_id: UUID) -> Subscription | None:
        """Lectura sin bloqueo: distingue un cambio concurrente de un alta en curso."""
        ...

    def pending_commitments(self, user_id: UUID) -> list[BillingCommitment]:
        """Compromisos `PENDING_EXTERNAL` del usuario, bloqueados."""
        ...

    def end_subscription(self, subscription: Subscription, ended_at: datetime) -> None: ...

    def add_subscription(self, subscription: Subscription) -> None: ...

    def add_commitment(self, commitment: BillingCommitment) -> None: ...

    def waive_commitment(self, commitment_id: UUID, waived_at: datetime) -> None: ...

    def add_movement(self, movement: Movement) -> None: ...

    def add_outbox(self, message: OutboxMessage) -> None: ...

    def add_idempotency(self, record: IdempotencyRecord) -> None: ...

    def commit(self) -> None:
        """Confirma todo o nada; traduce las unicidades a `IdempotencyKeyConflict` (misma
        clave confirmada antes) o `ActiveSubscriptionExists` (otra suscripción activa)."""
        ...
