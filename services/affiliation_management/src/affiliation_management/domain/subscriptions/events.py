"""`suscripcion.activada.v1` (ADR-024).

El payload no transporta nombres, precios ni la lista de capacidades: quien
necesite el estado vigente lo consulta por el contrato de capacidades.
"""

from typing import Any

from affiliation_management.domain.subscriptions.model import Subscription

AGGREGATE_TYPE = "user_subscription"
EVENT_TYPE = "suscripcion.activada.v1"
SCHEMA_VERSION = 1


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
