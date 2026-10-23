"""Dobles en memoria del cambio de plan (HU016): catálogo de ADR-036 y unidad de trabajo que
valida las mismas unicidades que PostgreSQL."""

import copy
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, Self
from uuid import UUID

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from affiliation_management.application.errors import IdempotencyKeyConflict
from affiliation_management.application.ports import (
    ActiveSubscriptionExists,
    IdempotencyRecord,
    OfferView,
    OutboxMessage,
    PlanView,
    SubscriptionView,
)
from affiliation_management.domain.catalog import CAPABILITIES_BY_PLAN
from affiliation_management.domain.subscriptions.change import (
    BillingCommitment,
    CatalogPlan,
    CommitmentStatus,
    Movement,
    PlanOffer,
)
from affiliation_management.domain.subscriptions.model import (
    Periodicity,
    PlanVersionRef,
    Subscription,
    SubscriptionStatus,
)
from sportapp_platform.tokens import AccessTokenVerifier, public_jwk

CATALOG = "plans-2026-10-13.v1"
NOW = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)
ISSUER, AUDIENCE = "sportapp-users-management", "sportapp"
SIGNER = Ed25519PrivateKey.generate()
VERIFIER = AccessTokenVerifier(
    {"keys": [public_jwk(SIGNER.public_key())]}, issuer=ISSUER, audience=AUDIENCE
)
# Ofertas de ADR-036 (precio de referencia en COP antes de impuestos).
PRICES = {
    ("FREE", Periodicity.NONE): (Decimal("0.00"), None),
    ("INTERMEDIATE", Periodicity.MONTHLY): (Decimal("2400.00"), 1),
    ("INTERMEDIATE", Periodicity.QUARTERLY): (Decimal("7200.00"), 3),
    ("INTERMEDIATE", Periodicity.ANNUAL): (Decimal("28800.00"), 12),
    ("PREMIUM", Periodicity.MONTHLY): (Decimal("4800.00"), 1),
    ("PREMIUM", Periodicity.QUARTERLY): (Decimal("14400.00"), 3),
    ("PREMIUM", Periodicity.ANNUAL): (Decimal("57600.00"), 12),
}
OFFER_IDS = {key: uuid.uuid5(uuid.NAMESPACE_URL, f"{key[0]}:{key[1]}") for key in PRICES}


def token(user_id: UUID, actor_type: str | None = "athlete") -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(user_id),
        "iat": now,
        "nbf": now,
        "exp": now + 900,
        "jti": str(uuid.uuid4()),
    }
    if actor_type is not None:
        claims["actor_type"] = actor_type
    return jwt.encode(
        claims, SIGNER, algorithm="EdDSA", headers={"kid": public_jwk(SIGNER.public_key())["kid"]}
    )


def plan_ref(plan_code: str) -> PlanVersionRef:
    return PlanVersionRef(plan_code=plan_code, version=2, catalog_version=CATALOG)


class InMemoryCatalog:
    """Catálogo vigente de ADR-036; `selectable` y ofertas se pueden alterar por prueba."""

    def __init__(self) -> None:
        self.selectable = {"FREE": False, "INTERMEDIATE": True, "PREMIUM": True}
        self.removed: set[tuple[str, Periodicity]] = set()

    def offer(self, plan_code: str, periodicity: Periodicity) -> PlanOffer:
        price, months = PRICES[(plan_code, periodicity)]
        return PlanOffer(
            offer_id=OFFER_IDS[(plan_code, periodicity)],
            plan=plan_ref(plan_code),
            periodicity=periodicity,
            price=price,
            currency="COP",
            duration_months=months,
            selectable=self.selectable[plan_code],
        )

    def current_plan(self, plan_code: str, now: datetime) -> CatalogPlan | None:
        if plan_code not in self.selectable:
            return None
        return CatalogPlan(
            plan_code=plan_code,
            selectable=self.selectable[plan_code],
            offers=tuple(
                self.offer(code, periodicity)
                for code, periodicity in PRICES
                if code == plan_code and (code, periodicity) not in self.removed
            ),
        )

    def catalog(self, catalog_version: str) -> list[PlanView]:
        return [
            PlanView(
                plan_code=code,
                name=code.title(),
                description=f"Plan {code}",
                level=level,
                version=2,
                selectable=self.selectable[code],
                capabilities=tuple(sorted(CAPABILITIES_BY_PLAN[code])),
                offers=tuple(
                    OfferView(
                        periodicity=periodicity.value,
                        price=PRICES[(code, periodicity)][0],
                        currency="COP",
                        duration_months=PRICES[(code, periodicity)][1],
                    )
                    for plan_code, periodicity in PRICES
                    if plan_code == code
                ),
            )
            for level, code in enumerate(("FREE", "INTERMEDIATE", "PREMIUM"), start=1)
        ]


@dataclass
class InMemorySubscriptionStore:
    subscriptions: dict[UUID, Subscription] = field(default_factory=dict)
    commitments: dict[UUID, BillingCommitment] = field(default_factory=dict)
    movements: list[Movement] = field(default_factory=list)
    outbox: list[OutboxMessage] = field(default_factory=list)
    idempotency: dict[tuple[UUID, str, UUID], IdempotencyRecord] = field(default_factory=dict)
    commits: int = 0

    def active(self, user_id: UUID) -> Subscription | None:
        return next(
            (
                s
                for s in self.subscriptions.values()
                if s.user_id == user_id and s.status is SubscriptionStatus.ACTIVE
            ),
            None,
        )

    def free_subscription(self, user_id: UUID, version: int = 1) -> Subscription:
        subscription = Subscription.activate_free(
            subscription_id=uuid.uuid4(),
            user_id=user_id,
            plan=plan_ref("FREE"),
            now=NOW,
            plan_offer_id=OFFER_IDS[("FREE", Periodicity.NONE)],
        )
        if version != 1:
            subscription = copy.replace(subscription, version=version)
        self.subscriptions[subscription.subscription_id] = subscription
        return subscription


class InMemorySubscriptionReader:
    def __init__(self, store: InMemorySubscriptionStore) -> None:
        self.store = store

    def active(self, user_id: UUID) -> SubscriptionView | None:
        current = self.store.active(user_id)
        if current is None:
            return None
        price = next(
            (
                PRICES[key][0]
                for key, offer_id in OFFER_IDS.items()
                if offer_id == current.plan_offer_id
            ),
            None,
        )
        return SubscriptionView(subscription=current, price=price)


class InMemoryChangeUnitOfWork:
    """Copia el estado al abrir y lo publica solo con `commit` (todo o nada)."""

    def __init__(
        self, store: InMemorySubscriptionStore, before_commit: Callable[[], None] | None = None
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self.draft = copy.deepcopy(store)

    def __enter__(self) -> Self:
        self.draft = copy.deepcopy(self.store)
        self.base_commits = self.store.commits
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def find_idempotency(
        self, actor_id: UUID, operation: str, key: UUID
    ) -> IdempotencyRecord | None:
        return self.draft.idempotency.get((actor_id, operation, key))

    def delete_idempotency(self, actor_id: UUID, operation: str, key: UUID) -> None:
        self.draft.idempotency.pop((actor_id, operation, key), None)

    def lock_active_subscription(self, user_id: UUID) -> Subscription | None:
        return self.draft.active(user_id)

    def active_subscription(self, user_id: UUID) -> Subscription | None:
        return self.store.active(user_id)

    def pending_commitments(self, user_id: UUID) -> list[BillingCommitment]:
        return [
            c
            for c in self.draft.commitments.values()
            if c.user_id == user_id and c.status is CommitmentStatus.PENDING_EXTERNAL
        ]

    def end_subscription(self, subscription: Subscription, ended_at: datetime) -> None:
        self.draft.subscriptions[subscription.subscription_id] = subscription

    def add_subscription(self, subscription: Subscription) -> None:
        self.draft.subscriptions[subscription.subscription_id] = subscription

    def add_commitment(self, commitment: BillingCommitment) -> None:
        self.draft.commitments[commitment.commitment_id] = commitment

    def waive_commitment(self, commitment_id: UUID, waived_at: datetime) -> None:
        commitment = self.draft.commitments[commitment_id]
        self.draft.commitments[commitment_id] = copy.replace(
            commitment, status=CommitmentStatus.WAIVED
        )

    def add_movement(self, movement: Movement) -> None:
        self.draft.movements.append(movement)

    def add_outbox(self, message: OutboxMessage) -> None:
        self.draft.outbox.append(message)

    def add_idempotency(self, record: IdempotencyRecord) -> None:
        self.draft.idempotency[(record.actor_id, record.operation, record.key)] = record
        self.pending_key = (record.actor_id, record.operation, record.key)

    def commit(self) -> None:
        if self.before_commit is not None:
            hook, self.before_commit = self.before_commit, None
            hook()
        if self.store.commits != self.base_commits:
            # Otra transacción confirmó en medio: misma clave o suscripción activa concurrente.
            if getattr(self, "pending_key", None) in self.store.idempotency:
                raise IdempotencyKeyConflict()
            raise ActiveSubscriptionExists()
        self.store.subscriptions = self.draft.subscriptions
        self.store.commitments = self.draft.commitments
        self.store.movements = self.draft.movements
        self.store.outbox = self.draft.outbox
        self.store.idempotency = self.draft.idempotency
        self.store.commits += 1
