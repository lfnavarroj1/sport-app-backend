"""HU016 (ADR-036): catálogo vigente y suscripción del deportista.

- `GET /v1/plans`: planes de la versión de catálogo esperada (la misma que exige la sonda de
  disponibilidad) con capacidades, ofertas, `selectable` y el `plan_code` actual de quien
  consulta (`FREE` tras el alta automática, sin selección manual; `null` en `PROVISIONING`).
- `GET /v1/me/subscription`: suscripción vigente o `PROVISIONING`.

Solo el deportista; los demás actores reciben `permission_required`.
"""

from dataclasses import dataclass
from typing import Any

from affiliation_management.application.errors import PermissionRequired
from affiliation_management.application.ports import (
    Caller,
    CatalogReader,
    PlanView,
    SubscriptionReader,
)
from affiliation_management.application.subscription_documents import (
    provisioning_document,
    subscription_document,
)
from affiliation_management.domain.subscriptions.model import ATHLETE


@dataclass(frozen=True)
class PlansCatalog:
    catalog_version: str
    current_plan_code: str | None
    plans: list[PlanView]


def _require_athlete(caller: Caller) -> None:
    if caller.actor_type != ATHLETE:
        raise PermissionRequired()


class ListPlans:
    def __init__(
        self, *, catalog: CatalogReader, subscriptions: SubscriptionReader, catalog_version: str
    ) -> None:
        self._catalog = catalog
        self._subscriptions = subscriptions
        self._catalog_version = catalog_version

    def __call__(self, caller: Caller) -> PlansCatalog:
        _require_athlete(caller)
        current = self._subscriptions.active(caller.user_id)
        return PlansCatalog(
            catalog_version=self._catalog_version,
            current_plan_code=current.subscription.plan.plan_code if current else None,
            plans=self._catalog.catalog(self._catalog_version),
        )


class GetMySubscription:
    def __init__(self, subscriptions: SubscriptionReader) -> None:
        self._subscriptions = subscriptions

    def __call__(self, caller: Caller) -> dict[str, Any]:
        _require_athlete(caller)
        current = self._subscriptions.active(caller.user_id)
        if current is None:
            return provisioning_document()
        return subscription_document(current.subscription, current.price)
