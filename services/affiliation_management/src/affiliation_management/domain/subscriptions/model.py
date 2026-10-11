"""Suscripción de un usuario a una versión de plan (ADR-024)."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

ATHLETE = "athlete"
KNOWN_ACTOR_TYPES = frozenset({ATHLETE, "organizer", "service_provider"})


class SubscriptionStatus(StrEnum):
    ACTIVE = "ACTIVE"


class SubscriptionOrigin(StrEnum):
    AUTO_REGISTRATION = "AUTO_REGISTRATION"


def receives_free_plan(actor_type: str) -> bool:
    """Solo el deportista recibe automáticamente el plan gratuito.

    `organizer` y `service_provider` no tienen suscripción de deportista.
    """
    return actor_type == ATHLETE


@dataclass(frozen=True)
class PlanVersionRef:
    """Versión concreta de un plan dentro de una versión del catálogo."""

    plan_code: str
    version: int
    catalog_version: str


@dataclass(frozen=True)
class Subscription:
    subscription_id: UUID
    user_id: UUID
    plan: PlanVersionRef
    status: SubscriptionStatus
    origin: SubscriptionOrigin
    started_at: datetime
    # Secuencia lógica de suscripción del usuario: `aggregate_version` de sus eventos.
    version: int

    @classmethod
    def activate_free(
        cls, *, subscription_id: UUID, user_id: UUID, plan: PlanVersionRef, now: datetime
    ) -> "Subscription":
        return cls(
            subscription_id=subscription_id,
            user_id=user_id,
            plan=plan,
            status=SubscriptionStatus.ACTIVE,
            origin=SubscriptionOrigin.AUTO_REGISTRATION,
            started_at=now,
            version=1,
        )
