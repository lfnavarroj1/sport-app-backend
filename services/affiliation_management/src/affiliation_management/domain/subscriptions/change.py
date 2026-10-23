"""HU016 (ADR-036): reglas para seleccionar o cambiar el plan de un deportista.

- El plan debe existir en el catálogo vigente (`plan_not_found`) y ser seleccionable
  (`plan_not_selectable`); volver a `FREE` siempre está permitido.
- `FREE` exige `periodicity = NONE` y los planes pagos `MONTHLY`, `QUARTERLY` o `ANNUAL`
  (`422`, `inconsistent_values`).
- Sin suscripción activa (alta `FREE` en curso) → `subscription_provisioning`.
- `expected_subscription_version` distinta de la vigente → `version_conflict`.
- Mismo plan y periodicidad vigentes → `subscription_unchanged`.

El cambio es inmediato y sin prorrateo: la suscripción vigente termina y nace otra `ACTIVE`
con `period_start = now` y `period_end = period_start + duration_months` (meses de
calendario). Un plan pago registra un compromiso de pago **informativo** `PENDING_EXTERNAL`
(SportApp no cobra); volver a `FREE` condona (`WAIVED`) los compromisos aún vigentes
(decisión del responsable del 2026-10-23: un cambio entre planes pagos conserva el anterior).
"""

import calendar
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from affiliation_management.domain.catalog import FREE_PLAN
from affiliation_management.domain.subscriptions.model import (
    Periodicity,
    PlanVersionRef,
    Subscription,
    SubscriptionOrigin,
    SubscriptionStatus,
)

PAID_PERIODICITIES = frozenset({Periodicity.MONTHLY, Periodicity.QUARTERLY, Periodicity.ANNUAL})
# Identificador determinista de `FREE_ACTIVATED`: el relleno de `0004_subscription_changes`
# y el consumidor del alta calculan el mismo, así que repetir el relleno no duplica.
MOVEMENT_NAMESPACE = uuid.UUID("a5d0c9e2-7f41-5b36-8e2d-4c1b9f6a3e70")


def free_activated_movement_id(subscription_id: UUID) -> UUID:
    return uuid.uuid5(MOVEMENT_NAMESPACE, f"FREE_ACTIVATED:{subscription_id}")


class ChangeRejected(Exception):
    code = "change_rejected"


class PlanNotFound(ChangeRejected):
    code = "plan_not_found"


class PlanNotSelectable(ChangeRejected):
    code = "plan_not_selectable"


class InconsistentPeriodicity(ChangeRejected):
    """La periodicidad no corresponde al plan: `422 validation_error`."""

    code = "validation_error"
    field = "body.periodicity"
    issue = "inconsistent_values"


class SubscriptionProvisioning(ChangeRejected):
    """El alta `FREE` aún no termina: recuperable, el cliente reintenta."""

    code = "subscription_provisioning"


class SubscriptionUnchanged(ChangeRejected):
    code = "subscription_unchanged"


class SubscriptionVersionConflict(ChangeRejected):
    code = "version_conflict"

    def __init__(self, current_version: int) -> None:
        super().__init__(f"current_version={current_version}")
        self.current_version = current_version


class CommitmentStatus(StrEnum):
    PENDING_EXTERNAL = "PENDING_EXTERNAL"
    WAIVED = "WAIVED"


class MovementType(StrEnum):
    """Movimientos del estado de cuenta (HU007 los lee)."""

    FREE_ACTIVATED = "FREE_ACTIVATED"
    PLAN_CHANGED = "PLAN_CHANGED"
    RETURNED_TO_FREE = "RETURNED_TO_FREE"
    COMMITMENT_CREATED = "COMMITMENT_CREATED"
    COMMITMENT_WAIVED = "COMMITMENT_WAIVED"


@dataclass(frozen=True)
class PlanOffer:
    """Oferta vigente de una versión de plan; `selectable` es el del plan."""

    offer_id: UUID
    plan: PlanVersionRef
    periodicity: Periodicity
    price: Decimal
    currency: str
    duration_months: int | None
    selectable: bool


@dataclass(frozen=True)
class CatalogPlan:
    """Plan del catálogo vigente con sus ofertas (las de su última versión vigente)."""

    plan_code: str
    selectable: bool
    offers: tuple[PlanOffer, ...]

    def offer_for(self, periodicity: Periodicity) -> PlanOffer | None:
        return next((offer for offer in self.offers if offer.periodicity is periodicity), None)


@dataclass(frozen=True)
class BillingCommitment:
    commitment_id: UUID
    subscription_id: UUID
    user_id: UUID
    plan_code: str
    plan_version: int
    plan_offer_id: UUID
    periodicity: Periodicity
    amount: Decimal
    currency: str
    period_start: datetime
    period_end: datetime
    # Pago anticipado (ADR-036): el compromiso vence al iniciar el periodo.
    due_at: datetime
    status: CommitmentStatus
    created_at: datetime


@dataclass(frozen=True)
class Movement:
    movement_id: UUID
    user_id: UUID
    movement_type: MovementType
    occurred_at: datetime
    subscription_id: UUID
    plan_code: str
    periodicity: Periodicity
    commitment_id: UUID | None = None


@dataclass(frozen=True)
class ChangeRequest:
    plan_code: str
    periodicity: Periodicity
    expected_subscription_version: int


def add_months(start: datetime, months: int) -> datetime:
    """Suma meses de calendario; el día se ajusta al último del mes si no existe."""
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return start.replace(year=year, month=month, day=day)


def choose_offer(plan: CatalogPlan | None, request: ChangeRequest) -> PlanOffer:
    """Elegibilidad del plan y coherencia de la periodicidad (sin mirar la suscripción)."""
    if plan is None:
        raise PlanNotFound()
    is_free = plan.plan_code == FREE_PLAN
    if is_free != (request.periodicity is Periodicity.NONE):
        raise InconsistentPeriodicity()
    if not is_free and not plan.selectable:
        raise PlanNotSelectable()
    offer = plan.offer_for(request.periodicity)
    if offer is None:
        # El catálogo vigente no ofrece esa periodicidad: el plan no es seleccionable así.
        raise PlanNotSelectable()
    return offer


def check_current(current: Subscription | None, request: ChangeRequest, offer: PlanOffer) -> None:
    """Estado de la suscripción vigente frente a la solicitud."""
    if current is None:
        raise SubscriptionProvisioning()
    if current.version != request.expected_subscription_version:
        raise SubscriptionVersionConflict(current.version)
    if current.plan.plan_code == offer.plan.plan_code and current.periodicity is offer.periodicity:
        raise SubscriptionUnchanged()


def replacement(
    current: Subscription, offer: PlanOffer, *, subscription_id: UUID, now: datetime
) -> tuple[Subscription, Subscription]:
    """La suscripción vigente terminada y la nueva `ACTIVE` (versión consecutiva)."""
    ended = Subscription(
        subscription_id=current.subscription_id,
        user_id=current.user_id,
        plan=current.plan,
        status=SubscriptionStatus.ENDED,
        origin=current.origin,
        started_at=current.started_at,
        version=current.version,
        periodicity=current.periodicity,
        plan_offer_id=current.plan_offer_id,
        period_start=current.period_start,
        period_end=current.period_end,
    )
    period_end = (
        add_months(now, offer.duration_months) if offer.duration_months is not None else None
    )
    created = Subscription(
        subscription_id=subscription_id,
        user_id=current.user_id,
        plan=offer.plan,
        status=SubscriptionStatus.ACTIVE,
        origin=SubscriptionOrigin.USER_CHANGE,
        started_at=now,
        version=current.version + 1,
        periodicity=offer.periodicity,
        plan_offer_id=offer.offer_id,
        period_start=now,
        period_end=period_end,
    )
    return ended, created


def commitment_for(
    subscription: Subscription, offer: PlanOffer, *, commitment_id: UUID, now: datetime
) -> BillingCommitment | None:
    """Compromiso informativo de un plan pago; `FREE` no genera compromisos."""
    if offer.periodicity is Periodicity.NONE:
        return None
    if subscription.period_start is None or subscription.period_end is None:
        raise ValueError("un plan pago siempre tiene periodo")
    return BillingCommitment(
        commitment_id=commitment_id,
        subscription_id=subscription.subscription_id,
        user_id=subscription.user_id,
        plan_code=offer.plan.plan_code,
        plan_version=offer.plan.version,
        plan_offer_id=offer.offer_id,
        periodicity=offer.periodicity,
        amount=offer.price,
        currency=offer.currency,
        period_start=subscription.period_start,
        period_end=subscription.period_end,
        due_at=subscription.period_start,
        status=CommitmentStatus.PENDING_EXTERNAL,
        created_at=now,
    )


def is_waivable(commitment: BillingCommitment, now: datetime) -> bool:
    """Al volver a `FREE` se condonan los compromisos pendientes cuyo periodo no terminó."""
    return commitment.status is CommitmentStatus.PENDING_EXTERNAL and commitment.period_end > now
