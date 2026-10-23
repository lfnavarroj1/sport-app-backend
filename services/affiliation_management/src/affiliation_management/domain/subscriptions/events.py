"""Eventos de la suscripción del usuario en `sportapp.afiliacion.v1`.

- `suscripcion.activada.v1` (ADR-024, HU001): alta automática del plan gratuito.
- `suscripcion.cambiada.v1` (ADR-036, HU016): cambio de plan elegido por el deportista.

Ambos usan `aggregate_type = user_subscription` y `aggregate_id = ordering_key = user_id`,
con `aggregate_version` consecutiva por usuario (la versión de la suscripción). El payload no
transporta nombres, precios ni la lista de capacidades: quien necesite el estado vigente lo
consulta por el contrato de capacidades.
"""

from datetime import UTC, datetime
from typing import Any

from affiliation_management.domain.subscriptions.model import Subscription

AGGREGATE_TYPE = "user_subscription"
EVENT_TYPE = "suscripcion.activada.v1"
CHANGED_EVENT_TYPE = "suscripcion.cambiada.v1"
SCHEMA_VERSION = 1


def utc_instant(value: datetime) -> str:
    """Instante en UTC con sufijo `Z` (el frontend convierte a la zona del usuario)."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def subscription_activated_payload(subscription: Subscription) -> dict[str, Any]:
    return {
        "subscription_id": str(subscription.subscription_id),
        "user_id": str(subscription.user_id),
        "plan_code": subscription.plan.plan_code,
        "plan_version": subscription.plan.version,
        "catalog_version": subscription.plan.catalog_version,
        "status": subscription.status.value,
        "origin": subscription.origin.value,
        "activated_at": subscription.started_at.isoformat(),
    }


def subscription_changed_payload(previous: Subscription, current: Subscription) -> dict[str, Any]:
    if current.period_start is None:
        raise ValueError("la suscripción nueva siempre tiene inicio de periodo")
    return {
        "subscription_id": str(current.subscription_id),
        "previous_subscription_id": str(previous.subscription_id),
        "user_id": str(current.user_id),
        "previous_plan_code": previous.plan.plan_code,
        "plan_code": current.plan.plan_code,
        "plan_version": current.plan.version,
        "periodicity": current.periodicity.value,
        "catalog_version": current.plan.catalog_version,
        "status": current.status.value,
        "origin": current.origin.value,
        "period_start": utc_instant(current.period_start),
        "period_end": utc_instant(current.period_end) if current.period_end else None,
        "changed_at": utc_instant(current.started_at),
    }
