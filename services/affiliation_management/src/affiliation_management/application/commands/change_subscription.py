"""HU016 (ADR-036): seleccionar o cambiar el plan del deportista.

Secuencia:
1. Solo `athlete` (`permission_required`).
2. Plan vigente, seleccionable y periodicidad coherente, sin tocar la suscripción
   (`plan_not_found`, `plan_not_selectable`, `422`).
3. Si la clave de idempotencia tiene un resultado vigente, se devuelve (24 h por deportista);
   con otro payload, `idempotency_key_reused`.
4. En una sola transacción: bloquear la suscripción activa, volver a comprobar la clave,
   verificar `PROVISIONING`, `expected_subscription_version` y `subscription_unchanged`,
   terminar la vigente, crear la nueva, registrar el compromiso informativo (o condonar los
   vigentes al volver a `FREE`), los movimientos, `suscripcion.cambiada.v1` y la clave.
5. Responder después del COMMIT. `GET /v1/me/capabilities` refleja el cambio porque el BFF
   consulta Afiliación en cada solicitud.

SportApp no cobra: los compromisos quedan `PENDING_EXTERNAL`. La auditoría no registra
importes.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from affiliation_management.application.errors import (
    IdempotencyKeyConflict,
    IdempotencyKeyReused,
    PermissionRequired,
)
from affiliation_management.application.ports import (
    ActiveSubscriptionExists,
    Caller,
    CatalogReader,
    Clock,
    IdempotencyRecord,
    OutboxMessage,
    SubscriptionChangeUnitOfWork,
)
from affiliation_management.application.subscription_documents import subscription_document
from affiliation_management.domain.catalog import FREE_PLAN
from affiliation_management.domain.subscriptions.change import (
    ChangeRejected,
    ChangeRequest,
    Movement,
    MovementType,
    PlanOffer,
    SubscriptionProvisioning,
    SubscriptionVersionConflict,
    check_current,
    choose_offer,
    commitment_for,
    is_waivable,
    replacement,
)
from affiliation_management.domain.subscriptions.events import (
    AGGREGATE_TYPE,
    CHANGED_EVENT_TYPE,
    SCHEMA_VERSION,
    subscription_changed_payload,
)
from affiliation_management.domain.subscriptions.model import ATHLETE, Subscription

OPERATION = "change_subscription"
IDEMPOTENCY_TTL = timedelta(hours=24)

_audit = logging.getLogger("sportapp.audit")


@dataclass(frozen=True)
class ChangeSubscriptionCommand:
    request: ChangeRequest
    idempotency_key: UUID
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class ChangeResult:
    document: dict[str, Any]
    replayed: bool = False


def request_fingerprint(user_id: UUID, request: ChangeRequest) -> str:
    canonical = json.dumps(
        [
            OPERATION,
            str(user_id),
            request.plan_code,
            request.periodicity.value,
            request.expected_subscription_version,
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ChangeSubscription:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], SubscriptionChangeUnitOfWork],
        catalog: CatalogReader,
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._catalog = catalog
        self._clock = clock
        self._new_id = new_id

    def __call__(self, caller: Caller, command: ChangeSubscriptionCommand) -> ChangeResult:
        try:
            result, previous_plan = self._execute(caller, command)
        except (PermissionRequired, ChangeRejected, IdempotencyKeyReused) as error:
            self._audit(caller, command, "rejected", reason=error.code)
            raise
        self._audit(
            caller,
            command,
            "replayed" if result.replayed else "changed",
            subscription_id=result.document["subscription_id"],
            previous_plan_code=previous_plan,
        )
        return result

    def _audit(
        self,
        caller: Caller,
        command: ChangeSubscriptionCommand,
        result: str,
        *,
        reason: str | None = None,
        subscription_id: str | None = None,
        previous_plan_code: str | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "result": result,
            "user_id": str(caller.user_id),
            "plan_code": command.request.plan_code,
            "periodicity": command.request.periodicity.value,
            "correlation_id": str(command.correlation_id) if command.correlation_id else None,
        }
        if reason is not None:
            fields["reason"] = reason
        if subscription_id is not None:
            fields["subscription_id"] = subscription_id
        if previous_plan_code is not None:
            fields["previous_plan_code"] = previous_plan_code
        _audit.info("audit.subscription_changed", extra={"fields": fields})

    def _execute(
        self, caller: Caller, command: ChangeSubscriptionCommand
    ) -> tuple[ChangeResult, str | None]:
        if caller.actor_type != ATHLETE:
            raise PermissionRequired()
        now = self._clock.now()
        offer = choose_offer(
            self._catalog.current_plan(command.request.plan_code, now), command.request
        )
        fingerprint = request_fingerprint(caller.user_id, command.request)
        previous = self._previous(caller.user_id, command.idempotency_key, fingerprint)
        if previous is not None:
            return previous, None
        try:
            return self._change(caller.user_id, command, offer, fingerprint)
        except IdempotencyKeyConflict:
            previous = self._previous(caller.user_id, command.idempotency_key, fingerprint)
            if previous is None:
                raise
            return previous, None
        except ActiveSubscriptionExists:
            # Otra transacción creó primero la suscripción activa: conflicto de versión.
            with self._unit_of_work() as unit_of_work:
                winner = unit_of_work.active_subscription(caller.user_id)
            raise SubscriptionVersionConflict(winner.version if winner else 0) from None

    def _previous(self, user_id: UUID, key: UUID, fingerprint: str) -> ChangeResult | None:
        with self._unit_of_work() as unit_of_work:
            record = unit_of_work.find_idempotency(user_id, OPERATION, key)
        if record is None or record.expires_at <= self._clock.now():
            return None
        if record.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused()
        return ChangeResult(document=record.response["body"], replayed=True)

    def _change(
        self,
        user_id: UUID,
        command: ChangeSubscriptionCommand,
        offer: PlanOffer,
        fingerprint: str,
    ) -> tuple[ChangeResult, str]:
        key = command.idempotency_key
        with self._unit_of_work() as unit_of_work:
            current = unit_of_work.lock_active_subscription(user_id)
            now = self._clock.now()
            existing = unit_of_work.find_idempotency(user_id, OPERATION, key)
            if existing is not None:
                if existing.expires_at > now:
                    raise IdempotencyKeyConflict()
                unit_of_work.delete_idempotency(user_id, OPERATION, key)
            if current is None:
                # Sin fila bloqueada: o el alta `FREE` no terminó, o un cambio concurrente
                # cerró la que se esperaba y ya existe otra activa.
                other = unit_of_work.active_subscription(user_id)
                if other is not None:
                    raise SubscriptionVersionConflict(other.version)
                raise SubscriptionProvisioning()
            check_current(current, command.request, offer)
            ended, created = replacement(current, offer, subscription_id=self._new_id(), now=now)
            unit_of_work.end_subscription(ended, now)
            unit_of_work.add_subscription(created)
            self._record_movements(unit_of_work, created, offer, now)
            unit_of_work.add_outbox(
                OutboxMessage(
                    event_id=self._new_id(),
                    event_type=CHANGED_EVENT_TYPE,
                    schema_version=SCHEMA_VERSION,
                    aggregate_type=AGGREGATE_TYPE,
                    aggregate_id=user_id,
                    aggregate_version=created.version,
                    occurred_at=now,
                    correlation_id=command.correlation_id,
                    causation_id=str(key),
                    payload=subscription_changed_payload(ended, created),
                )
            )
            document = subscription_document(created, offer.price)
            unit_of_work.add_idempotency(
                IdempotencyRecord(
                    actor_id=user_id,
                    operation=OPERATION,
                    key=key,
                    request_fingerprint=fingerprint,
                    resource_id=created.subscription_id,
                    response={"status": 200, "body": document},
                    created_at=now,
                    expires_at=now + IDEMPOTENCY_TTL,
                )
            )
            unit_of_work.commit()
        return ChangeResult(document=document), current.plan.plan_code

    def _record_movements(
        self,
        unit_of_work: SubscriptionChangeUnitOfWork,
        created: Subscription,
        offer: PlanOffer,
        now: datetime,
    ) -> None:
        returned_to_free = offer.plan.plan_code == FREE_PLAN
        unit_of_work.add_movement(
            Movement(
                movement_id=self._new_id(),
                user_id=created.user_id,
                movement_type=(
                    MovementType.RETURNED_TO_FREE if returned_to_free else MovementType.PLAN_CHANGED
                ),
                occurred_at=now,
                subscription_id=created.subscription_id,
                plan_code=offer.plan.plan_code,
                periodicity=offer.periodicity,
            )
        )
        if returned_to_free:
            for pending in unit_of_work.pending_commitments(created.user_id):
                if not is_waivable(pending, now):
                    continue
                unit_of_work.waive_commitment(pending.commitment_id, now)
                unit_of_work.add_movement(
                    Movement(
                        movement_id=self._new_id(),
                        user_id=created.user_id,
                        movement_type=MovementType.COMMITMENT_WAIVED,
                        occurred_at=now,
                        subscription_id=pending.subscription_id,
                        plan_code=pending.plan_code,
                        periodicity=pending.periodicity,
                        commitment_id=pending.commitment_id,
                    )
                )
            return
        commitment = commitment_for(created, offer, commitment_id=self._new_id(), now=now)
        if commitment is None:
            return
        unit_of_work.add_commitment(commitment)
        unit_of_work.add_movement(
            Movement(
                movement_id=self._new_id(),
                user_id=created.user_id,
                movement_type=MovementType.COMMITMENT_CREATED,
                occurred_at=now,
                subscription_id=created.subscription_id,
                plan_code=commitment.plan_code,
                periodicity=commitment.periodicity,
                commitment_id=commitment.commitment_id,
            )
        )
