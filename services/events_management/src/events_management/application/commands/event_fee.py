"""HU025: asignar o retirar la tarifa informativa de un evento propio (ADR-040).

Secuencia (`PUT` y `DELETE` comparten el caso de uso; la operación se deriva de la tarifa):
1. Verificar que quien llama sea `organizer` con `events.pricing.manage`.
2. Si la clave de idempotencia del organizador para la operación ya tiene un resultado
   vigente, devolverlo; con otra solicitud, `idempotency_key_reused` (CA5).
3. En una sola transacción de escritura, con la fila del evento bloqueada:
   - inexistente o de otro organizador → `event_not_found`, sin cambios (CA3);
   - `expected_version` distinta de la vigente → `version_conflict` (CA3);
   - `starts_at` ya alcanzado → `422` `event_already_started` (CA2);
   - en otro caso, tarifa nueva (o `null`), versión + 1, `evento.tarifa_actualizada.v1` en la
     outbox y el resultado idempotente con `registration_count` (CA1, CA4).
Las inscripciones existentes no se tocan (conservan su `applied_fee`) y no hay cobro, estado
de pago ni reembolso (CA5). La respuesta se emite después del COMMIT y no espera la
proyección.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from events_management.application.errors import (
    IdempotencyKeyConflict,
    IdempotencyKeyReused,
    PermissionRequired,
)
from events_management.application.messages import parse_event_aggregate_document
from events_management.application.ports import (
    Caller,
    Clock,
    FeeUnitOfWork,
    IdempotencyRecord,
    OutboxMessage,
    PermissionPolicy,
)
from events_management.domain.sport_events.fee import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    FeeChange,
    VersionConflict,
    fee_document,
)
from events_management.domain.sport_events.model import EventNotFound, SportEvent
from events_management.domain.sport_events.published import (
    aggregate_document,
    owned_event_document,
)
from events_management.domain.validation import InvalidSportEvent

PUT_OPERATION = "put_event_fee"
DELETE_OPERATION = "delete_event_fee"
ORGANIZER = "organizer"
PRICING_PERMISSION = "events.pricing.manage"
IDEMPOTENCY_TTL = timedelta(hours=24)

_audit = logging.getLogger("sportapp.audit")
_REJECTIONS = (
    PermissionRequired,
    EventNotFound,
    InvalidSportEvent,
    VersionConflict,
    IdempotencyKeyReused,
)


@dataclass(frozen=True)
class ChangeEventFeeCommand:
    event_id: UUID
    change: FeeChange
    idempotency_key: UUID
    correlation_id: UUID | None = None

    @property
    def operation(self) -> str:
        return DELETE_OPERATION if self.change.fee is None else PUT_OPERATION


@dataclass(frozen=True)
class FeeChangeResult:
    event: SportEvent
    registration_count: int
    replayed: bool = False

    def document(self) -> dict[str, Any]:
        return owned_event_document(self.event, self.registration_count)


def request_fingerprint(organizer_id: UUID, command: ChangeEventFeeCommand) -> str:
    """Huella de organizador + operación + evento + tarifa + versión esperada: la misma
    clave con otro contenido nunca comparte resultado."""
    canonical = json.dumps(
        [
            command.operation,
            str(organizer_id),
            str(command.event_id),
            fee_document(command.change.fee),
            command.change.expected_version,
        ],
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _result(document: dict[str, Any]) -> FeeChangeResult:
    count = document.get("registration_count")
    if not isinstance(count, int) or isinstance(count, bool):
        raise ValueError("registration_count ausente en el resultado idempotente")
    return FeeChangeResult(parse_event_aggregate_document(document), count, replayed=True)


class ChangeEventFee:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], FeeUnitOfWork],
        permissions: PermissionPolicy,
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._permissions = permissions
        self._clock = clock
        self._new_id = new_id

    def authorize(self, caller: Caller) -> None:
        if caller.actor_type != ORGANIZER or not self._permissions.allows(
            caller.actor_type, PRICING_PERMISSION
        ):
            raise PermissionRequired()

    def __call__(self, caller: Caller, command: ChangeEventFeeCommand) -> FeeChangeResult:
        try:
            result = self._execute(caller, command)
        except _REJECTIONS as error:
            self._audit(caller, command, result="rejected", reason=error.code)
            raise
        self._audit(caller, command, result="replayed" if result.replayed else "changed")
        return result

    @staticmethod
    def _audit(caller: Caller, command: ChangeEventFeeCommand, **fields: str) -> None:
        # Sin importes ni textos libres: solo identificadores y resultado.
        _audit.info(
            "audit.event_fee_changed",
            extra={
                "fields": {
                    **fields,
                    "operation": command.operation,
                    "event_id": str(command.event_id),
                    "organizer_id": str(caller.user_id),
                    "correlation_id": str(command.correlation_id)
                    if command.correlation_id
                    else None,
                }
            },
        )

    def _execute(self, caller: Caller, command: ChangeEventFeeCommand) -> FeeChangeResult:
        self.authorize(caller)
        fingerprint = request_fingerprint(caller.user_id, command)
        previous = self._previous(caller.user_id, command, fingerprint)
        if previous is not None:
            return previous
        try:
            return self._change(caller.user_id, command, fingerprint)
        except IdempotencyKeyConflict:
            # Un reintento concurrente con la misma clave confirmó primero: su resultado.
            previous = self._previous(caller.user_id, command, fingerprint)
            if previous is None:
                raise
            return previous

    def _previous(
        self, organizer_id: UUID, command: ChangeEventFeeCommand, fingerprint: str
    ) -> FeeChangeResult | None:
        with self._unit_of_work() as unit_of_work:
            record = unit_of_work.find_idempotency(
                organizer_id, command.operation, command.idempotency_key
            )
        if record is None or record.expires_at <= self._clock.now():
            return None
        if record.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused()
        return _result(record.response)

    def _change(
        self, organizer_id: UUID, command: ChangeEventFeeCommand, fingerprint: str
    ) -> FeeChangeResult:
        now = self._clock.now()
        operation, key = command.operation, command.idempotency_key
        with self._unit_of_work() as unit_of_work:
            event = unit_of_work.lock_event(command.event_id)
            if event is None or event.organizer_id != organizer_id:
                raise EventNotFound()
            # Con el evento ya bloqueado: un reintento concurrente con la misma clave que
            # confirmó primero se devuelve como resultado original (CA5).
            stale = unit_of_work.find_idempotency(organizer_id, operation, key)
            if stale is not None:
                if stale.expires_at > now:
                    raise IdempotencyKeyConflict()
                unit_of_work.delete_idempotency(organizer_id, operation, key)
            event = event.change_fee(command.change, now)
            unit_of_work.update_fee(event)
            unit_of_work.add_outbox(
                OutboxMessage(
                    event_id=self._new_id(),
                    event_type=EVENT_TYPE,
                    schema_version=SCHEMA_VERSION,
                    aggregate_type=AGGREGATE_TYPE,
                    aggregate_id=event.event_id,
                    aggregate_version=event.version,
                    occurred_at=event.updated_at,
                    correlation_id=command.correlation_id,
                    causation_id=str(key),
                    payload=aggregate_document(event),
                )
            )
            result = FeeChangeResult(event, unit_of_work.count_registrations(event.event_id))
            unit_of_work.add_idempotency(
                IdempotencyRecord(
                    actor_id=organizer_id,
                    key=key,
                    operation=operation,
                    request_fingerprint=fingerprint,
                    resource_id=event.event_id,
                    response=result.document(),
                    created_at=now,
                    expires_at=now + IDEMPOTENCY_TTL,
                )
            )
            unit_of_work.commit()
        return result
