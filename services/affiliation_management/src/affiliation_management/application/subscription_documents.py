"""Documento JSON de la suscripción vigente (ADR-036): respuesta de `GET /v1/me/subscription`
y de `POST /v1/me/subscription/changes`, y resultado guardado para los reintentos.

Importes como cadena decimal con dos decimales e instantes en UTC con sufijo `Z`. Sin
suscripción (alta `FREE` en curso) el estado es `PROVISIONING` y el resto `null` (decisión
del responsable del 2026-10-23).
"""

from decimal import Decimal
from typing import Any

from affiliation_management.domain.subscriptions.events import utc_instant
from affiliation_management.domain.subscriptions.model import Subscription

PROVISIONING = "PROVISIONING"
_FIELDS = (
    "subscription_id",
    "plan_code",
    "plan_version",
    "periodicity",
    "price",
    "period_start",
    "period_end",
    "origin",
    "version",
)


def format_amount(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def subscription_document(subscription: Subscription, price: Decimal | None) -> dict[str, Any]:
    return {
        "subscription_id": str(subscription.subscription_id),
        "plan_code": subscription.plan.plan_code,
        "plan_version": subscription.plan.version,
        "periodicity": subscription.periodicity.value,
        "price": format_amount(price) if price is not None else None,
        "period_start": utc_instant(subscription.period_start)
        if subscription.period_start
        else None,
        "period_end": utc_instant(subscription.period_end) if subscription.period_end else None,
        "status": subscription.status.value,
        "origin": subscription.origin.value,
        "version": subscription.version,
    }


def provisioning_document() -> dict[str, Any]:
    return {**dict.fromkeys(_FIELDS), "status": PROVISIONING}
