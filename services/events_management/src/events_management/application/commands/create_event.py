"""HU021: crear el borrador de un evento masivo (ADR-028).

Secuencia:
1. Verificar que quien llama sea `organizer` con el permiso `events.create` (CA2).
2. Validar y normalizar el evento sin tocar persistencia (CA3).
3. Si la clave de idempotencia del organizador ya tiene un resultado vigente,
   devolverlo; con otro payload, `idempotency_key_reused` (CA4).
4. En una sola transacción de escritura: evento `DRAFT` versión 1, `evento.creado.v1`
   en la outbox y el resultado idempotente (CA1, CA5).
La respuesta se emite después del COMMIT y no espera la proyección de lectura.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from events_management.application.errors import (
    IdempotencyKeyConflict,
    IdempotencyKeyReused,
    PermissionRequired,
)
from events_management.application.messages import parse_event_document
from events_management.application.ports import (
    Caller,
    Clock,
    EventCreationUnitOfWork,
    IdempotencyRecord,
    OutboxMessage,
    PermissionPolicy,
)
from events_management.domain.sport_events.created import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    event_document,
)
from events_management.domain.sport_events.model import (
    EventDetails,
    EventInput,
    SportEvent,
    validate_event,
)
from events_management.domain.validation import InvalidSportEvent

OPERATION = "create_event"
ORGANIZER = "organizer"
CREATE_PERMISSION = "events.create"
IDEMPOTENCY_TTL = timedelta(hours=24)

_audit = logging.getLogger("sportapp.audit")


@dataclass(frozen=True)
class CreateEventCommand:
    event: EventInput
    idempotency_key: UUID
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class CreatedEvent:
    event: SportEvent
    replayed: bool = False


def request_fingerprint(organizer_id: UUID, details: EventDetails) -> str:
    """Huella del payload normalizado y del organizador autenticado: la misma clave de
    otro organizador nunca comparte resultado."""
    canonical = json.dumps(
        [
            OPERATION,
            str(organizer_id),
            details.name,
            details.description,
            details.discipline,
            [
                details.location.name,
                details.location.address,
                details.location.city,
                details.location.country_code,
            ],
            details.starts_at.isoformat(),
            details.time_zone,
            details.duration_minutes,
            details.capacity,
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CreateEvent:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], EventCreationUnitOfWork],
        permissions: PermissionPolicy,
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._permissions = permissions
        self._clock = clock
        self._new_id = new_id

    def __call__(self, caller: Caller, command: CreateEventCommand) -> CreatedEvent:
        try:
            result = self._execute(caller, command)
        except (PermissionRequired, InvalidSportEvent, IdempotencyKeyReused) as error:
            self._audit(caller, command, result="rejected", reason=error.code)
            raise
        self._audit(
            caller,
            command,
            result="replayed" if result.replayed else "created",
            event_id=str(result.event.event_id),
        )
        return result

    def validate(self, caller: Caller, event: EventInput) -> EventDetails:
        """Actor, permiso y reglas del evento, sin persistir; también para informar todos
        los errores cuando la solicitud ya es inválida por otra razón (p. ej., la clave)."""
        if caller.actor_type != ORGANIZER or not self._permissions.allows(
            caller.actor_type, CREATE_PERMISSION
        ):
            raise PermissionRequired()
        return validate_event(event, self._clock.now())

    @staticmethod
    def _audit(caller: Caller, command: CreateEventCommand, **fields: str) -> None:
        # Sin cuerpo libre, ubicación ni token (ADR-028, datos y seguridad).
        _audit.info(
            "audit.event_created",
            extra={
                "fields": {
                    **fields,
                    "organizer_id": str(caller.user_id),
                    "correlation_id": str(command.correlation_id)
                    if command.correlation_id
                    else None,
                }
            },
        )

    def _execute(self, caller: Caller, command: CreateEventCommand) -> CreatedEvent:
        details = self.validate(caller, command.event)
        fingerprint = request_fingerprint(caller.user_id, details)

        previous = self._previous(caller.user_id, command.idempotency_key, fingerprint)
        if previous is not None:
            return previous
        event = SportEvent.draft(
            event_id=self._new_id(),
            organizer_id=caller.user_id,
            details=details,
            now=self._clock.now(),
        )
        try:
            return self._create(command, event, fingerprint)
        except IdempotencyKeyConflict:
            # Una solicitud concurrente con la misma clave confirmó primero: su resultado.
            previous = self._previous(caller.user_id, command.idempotency_key, fingerprint)
            if previous is None:
                raise
            return previous

    def _previous(self, organizer_id: UUID, key: UUID, fingerprint: str) -> CreatedEvent | None:
        with self._unit_of_work() as unit_of_work:
            record = unit_of_work.find_idempotency(organizer_id, OPERATION, key)
        if record is None or record.expires_at <= self._clock.now():
            return None
        if record.operation != OPERATION or record.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused()
        return CreatedEvent(parse_event_document(record.response), replayed=True)

    def _create(
        self, command: CreateEventCommand, event: SportEvent, fingerprint: str
    ) -> CreatedEvent:
        now = event.created_at
        document = event_document(event)
        with self._unit_of_work() as unit_of_work:
            existing = unit_of_work.find_idempotency(
                event.organizer_id, OPERATION, command.idempotency_key
            )
            if existing is not None:
                if existing.expires_at > now:
                    raise IdempotencyKeyConflict()
                unit_of_work.delete_idempotency(
                    event.organizer_id, OPERATION, command.idempotency_key
                )
            unit_of_work.add_event(event)
            unit_of_work.add_outbox(
                OutboxMessage(
                    event_id=self._new_id(),
                    event_type=EVENT_TYPE,
                    schema_version=SCHEMA_VERSION,
                    aggregate_type=AGGREGATE_TYPE,
                    aggregate_id=event.event_id,
                    aggregate_version=event.version,
                    occurred_at=now,
                    correlation_id=command.correlation_id,
                    causation_id=str(command.idempotency_key),
                    payload=document,
                )
            )
            unit_of_work.add_idempotency(
                IdempotencyRecord(
                    actor_id=event.organizer_id,
                    key=command.idempotency_key,
                    operation=OPERATION,
                    request_fingerprint=fingerprint,
                    resource_id=event.event_id,
                    response=document,
                    created_at=now,
                    expires_at=now + IDEMPOTENCY_TTL,
                )
            )
            unit_of_work.commit()
        return CreatedEvent(event)
