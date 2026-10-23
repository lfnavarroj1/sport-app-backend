"""HU001 (ADR-024): aprovisionar el plan gratuito al consumir `usuario.registrado.v1`.

En una sola transacción local:
1. si el `event_id` ya está en la inbox, es una entrega repetida: no hace nada (CA9);
2. si el actor no es `athlete`, registra el evento sin suscripción (CA7);
3. si el usuario ya tiene una suscripción activa, registra el evento sin crear otra (CA9);
4. en otro caso crea la suscripción `FREE` con origen `AUTO_REGISTRATION`, deja
   `suscripcion.activada.v1` en la outbox y registra el evento en la inbox (CA6). Desde
   HU016 (ADR-036) la suscripción guarda la oferta `NONE` y el periodo, y el alta deja el
   movimiento `FREE_ACTIVATED` en `subscription_history`.

El consumidor confirma (ACK) el mensaje solo después de que este caso de uso
termina; un error deja el mensaje sin confirmar para que Pub/Sub lo reintente.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from affiliation_management.application.messages import USER_REGISTERED, RegisteredUser
from affiliation_management.application.ports import (
    ActiveSubscriptionExists,
    Clock,
    DuplicateDelivery,
    InboxRecord,
    InboxResult,
    OutboxMessage,
    ProvisioningUnitOfWork,
)
from affiliation_management.domain.subscriptions.change import (
    Movement,
    MovementType,
    free_activated_movement_id,
)
from affiliation_management.domain.subscriptions.events import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    subscription_activated_payload,
)
from affiliation_management.domain.subscriptions.model import Subscription, receives_free_plan

_audit = logging.getLogger("sportapp.audit")


class CatalogUnavailable(Exception):
    """Falta el plan predeterminado del catálogo: el evento se reintenta más tarde."""


class ProvisioningOutcome(StrEnum):
    SUBSCRIPTION_CREATED = "subscription_created"
    NOT_APPLICABLE = "not_applicable"
    ALREADY_ACTIVE = "already_active"
    DUPLICATE = "duplicate"


class ProvisionFreeSubscription:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], ProvisioningUnitOfWork],
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._new_id = new_id

    def __call__(self, event: RegisteredUser) -> ProvisioningOutcome:
        try:
            outcome = self._provision(event)
        except DuplicateDelivery:
            outcome = ProvisioningOutcome.DUPLICATE
        except ActiveSubscriptionExists:
            # Otra transacción activó al usuario primero; se deja constancia de este evento.
            outcome = self._record_only(event, InboxResult.ALREADY_ACTIVE)
        _audit.info(
            "audit.free_plan_provisioning",
            extra={
                "fields": {
                    "result": outcome.value,
                    "user_id": str(event.user_id),
                    "event_id": str(event.event_id),
                }
            },
        )
        return outcome

    def _provision(self, event: RegisteredUser) -> ProvisioningOutcome:
        now = self._clock.now()
        with self._unit_of_work() as unit_of_work:
            if unit_of_work.inbox_contains(event.event_id):
                return ProvisioningOutcome.DUPLICATE
            if not receives_free_plan(event.actor_type):
                unit_of_work.add_inbox(self._inbox(event, InboxResult.NOT_APPLICABLE, now))
                unit_of_work.commit()
                return ProvisioningOutcome.NOT_APPLICABLE
            if unit_of_work.has_active_subscription(event.user_id):
                unit_of_work.add_inbox(self._inbox(event, InboxResult.ALREADY_ACTIVE, now))
                unit_of_work.commit()
                return ProvisioningOutcome.ALREADY_ACTIVE
            plan = unit_of_work.default_athlete_plan(now)
            if plan is None:
                raise CatalogUnavailable()

            subscription = Subscription.activate_free(
                subscription_id=self._new_id(),
                user_id=event.user_id,
                plan=plan,
                now=now,
                plan_offer_id=unit_of_work.none_offer_id(plan),
            )
            unit_of_work.add_subscription(subscription)
            unit_of_work.add_movement(
                Movement(
                    movement_id=free_activated_movement_id(subscription.subscription_id),
                    user_id=event.user_id,
                    movement_type=MovementType.FREE_ACTIVATED,
                    occurred_at=now,
                    subscription_id=subscription.subscription_id,
                    plan_code=plan.plan_code,
                    periodicity=subscription.periodicity,
                )
            )
            unit_of_work.add_outbox(
                OutboxMessage(
                    event_id=self._new_id(),
                    event_type=EVENT_TYPE,
                    schema_version=SCHEMA_VERSION,
                    aggregate_type=AGGREGATE_TYPE,
                    # Clave de orden y agregado: el usuario (una secuencia de suscripción).
                    aggregate_id=event.user_id,
                    aggregate_version=subscription.version,
                    occurred_at=now,
                    correlation_id=event.correlation_id,
                    causation_id=str(event.event_id),
                    payload=subscription_activated_payload(subscription),
                )
            )
            unit_of_work.add_inbox(self._inbox(event, InboxResult.SUBSCRIPTION_CREATED, now))
            unit_of_work.commit()
        return ProvisioningOutcome.SUBSCRIPTION_CREATED

    def _record_only(self, event: RegisteredUser, result: InboxResult) -> ProvisioningOutcome:
        try:
            with self._unit_of_work() as unit_of_work:
                unit_of_work.add_inbox(self._inbox(event, result, self._clock.now()))
                unit_of_work.commit()
        except DuplicateDelivery:
            return ProvisioningOutcome.DUPLICATE
        return ProvisioningOutcome(result.value)

    @staticmethod
    def _inbox(event: RegisteredUser, result: InboxResult, now: datetime) -> InboxRecord:
        return InboxRecord(
            event_id=event.event_id,
            event_type=USER_REGISTERED,
            user_id=event.user_id,
            result=result,
            processed_at=now,
        )
