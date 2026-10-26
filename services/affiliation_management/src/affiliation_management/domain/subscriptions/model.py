"""Suscripción de un usuario a una versión de plan (ADR-024 y ADR-036)."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

ATHLETE = "athlete"
KNOWN_ACTOR_TYPES = frozenset({ATHLETE, "organizer", "service_provider"})


class SubscriptionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    # HU016: la suscripción anterior termina al cambiar de plan (nunca se borra).
    ENDED = "ENDED"


class SubscriptionOrigin(StrEnum):
    AUTO_REGISTRATION = "AUTO_REGISTRATION"
    USER_CHANGE = "USER_CHANGE"


class Periodicity(StrEnum):
    """`NONE` solo para `FREE`; los planes pagos usan las otras tres (ADR-036)."""

    NONE = "NONE"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    ANNUAL = "ANNUAL"


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
    # HU016 (ADR-036): oferta aceptada y periodo. `FREE` tiene periodicidad `NONE` y periodo
    # sin fin; `plan_offer_id` es nulo para las suscripciones a versiones sin ofertas
    # (`FREE` v1 de `plans-2026-10-11.v1`).
    periodicity: Periodicity = Periodicity.NONE
    plan_offer_id: UUID | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None

    @classmethod
    def activate_free(
        cls,
        *,
        subscription_id: UUID,
        user_id: UUID,
        plan: PlanVersionRef,
        now: datetime,
        plan_offer_id: UUID | None = None,
    ) -> "Subscription":
        return cls(
            subscription_id=subscription_id,
            user_id=user_id,
            plan=plan,
            status=SubscriptionStatus.ACTIVE,
            origin=SubscriptionOrigin.AUTO_REGISTRATION,
            started_at=now,
            version=1,
            periodicity=Periodicity.NONE,
            plan_offer_id=plan_offer_id,
            period_start=now,
            period_end=None,
        )
