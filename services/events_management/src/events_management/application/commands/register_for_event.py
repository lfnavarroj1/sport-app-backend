"""HU041: inscribir al deportista autenticado en un evento publicado (ADR-032).

Secuencia:
1. Solo `athlete` (CA9); el deportista sale del token.
2. Clave de idempotencia por deportista y operación (24 h): misma clave y evento →
   resultado original; otro evento → `idempotency_key_reused` (CA6).
3. En una sola transacción de escritura, con la fila del evento bloqueada: evento
   `PUBLISHED` (CA2), una inscripción por deportista (CA4), inicio futuro (CA3) y cupo
   (CA5); inscripción `REGISTERED` versión 1, `usuario.inscrito.v1` en la outbox y el
   resultado idempotente (CA1, CA7).
La respuesta se emite después del COMMIT y no espera la proyección.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from events_management.application.errors import (
    EventRegistrationExists,
    IdempotencyKeyConflict,
    IdempotencyKeyReused,
    PermissionRequired,
    RegistrationConflict,
)
from events_management.application.ports import (
    Caller,
    Clock,
    IdempotencyRecord,
    OutboxMessage,
    RegistrationUnitOfWork,
    RegistrationView,
)
from events_management.application.registration_documents import (
    registration_document,
    registration_from_document,
)
from events_management.domain.registrations.events import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    registered_payload,
)
from events_management.domain.registrations.model import (
    EventFull,
    EventNotAvailable,
    EventRegistrationClosed,
    register,
)

OPERATION = "register_for_event"
ATHLETE = "athlete"
IDEMPOTENCY_TTL = timedelta(hours=24)

_audit = logging.getLogger("sportapp.audit")
_REJECTIONS = (
    PermissionRequired,
    EventNotAvailable,
    EventRegistrationClosed,
    EventFull,
    EventRegistrationExists,
    IdempotencyKeyReused,
)


@dataclass(frozen=True)
class RegisterForEventCommand:
    event_id: UUID
    idempotency_key: UUID
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class RegistrationResult:
    registration: RegistrationView
    replayed: bool = False


def request_fingerprint(athlete_id: UUID, event_id: UUID) -> str:
    canonical = json.dumps([OPERATION, str(athlete_id), str(event_id)])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RegisterForEvent:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], RegistrationUnitOfWork],
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._new_id = new_id

    def __call__(self, caller: Caller, command: RegisterForEventCommand) -> RegistrationResult:
        try:
            result = self._execute(caller, command)
        except _REJECTIONS as error:
            self._audit(caller, command, result="rejected", reason=error.code)
            raise
        self._audit(
            caller,
            command,
            result="replayed" if result.replayed else "registered",
            registration_id=str(result.registration.registration.registration_id),
        )
        return result

    @staticmethod
    def _audit(caller: Caller, command: RegisterForEventCommand, **fields: str) -> None:
        # Sin cuerpos ni token (ADR-032, datos y seguridad).
        _audit.info(
            "audit.event_registration_created",
            extra={
                "fields": {
                    **fields,
                    "event_id": str(command.event_id),
                    "athlete_id": str(caller.user_id),
                    "correlation_id": str(command.correlation_id)
                    if command.correlation_id
                    else None,
                }
            },
        )

    def _execute(self, caller: Caller, command: RegisterForEventCommand) -> RegistrationResult:
        if caller.actor_type != ATHLETE:
            raise PermissionRequired()
        fingerprint = request_fingerprint(caller.user_id, command.event_id)
        previous = self._previous(caller.user_id, command.idempotency_key, fingerprint)
        if previous is not None:
            return previous
        try:
            return self._register(caller.user_id, command, fingerprint)
        except IdempotencyKeyConflict:
            previous = self._previous(caller.user_id, command.idempotency_key, fingerprint)
            if previous is None:
                raise
            return previous
        except RegistrationConflict:
            # Otra solicitud del mismo deportista inscribió primero: se informa la existente.
            with self._unit_of_work() as unit_of_work:
                existing = unit_of_work.find_registration(command.event_id, caller.user_id)
            if existing is None:
                raise
            raise EventRegistrationExists(existing.registration_id) from None

    def _previous(self, athlete_id: UUID, key: UUID, fingerprint: str) -> RegistrationResult | None:
        with self._unit_of_work() as unit_of_work:
            record = unit_of_work.find_idempotency(athlete_id, OPERATION, key)
        if record is None or record.expires_at <= self._clock.now():
            return None
        if record.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused()
        return RegistrationResult(
            registration_from_document(record.response, athlete_id), replayed=True
        )

    def _register(
        self, athlete_id: UUID, command: RegisterForEventCommand, fingerprint: str
    ) -> RegistrationResult:
        now = self._clock.now()
        with self._unit_of_work() as unit_of_work:
            event = unit_of_work.lock_event(command.event_id)
            # Con el evento ya bloqueado: un reintento concurrente con la misma clave que
            # confirmó primero se devuelve como resultado original, no como duplicado (CA6).
            stale = unit_of_work.find_idempotency(athlete_id, OPERATION, command.idempotency_key)
            if stale is not None:
                if stale.expires_at > now:
                    raise IdempotencyKeyConflict()
                unit_of_work.delete_idempotency(athlete_id, OPERATION, command.idempotency_key)
            if event is not None:
                existing = unit_of_work.find_registration(command.event_id, athlete_id)
                if existing is not None:
                    raise EventRegistrationExists(existing.registration_id)
            registration = register(
                registration_id=self._new_id(),
                athlete_id=athlete_id,
                event=event,
                registered_count=unit_of_work.count_registrations(command.event_id)
                if event is not None
                else 0,
                now=now,
            )
            view = RegistrationView(registration, unit_of_work.event_summary(command.event_id))
            unit_of_work.add_registration(registration)
            unit_of_work.add_outbox(
                OutboxMessage(
                    event_id=self._new_id(),
                    event_type=EVENT_TYPE,
                    schema_version=SCHEMA_VERSION,
                    aggregate_type=AGGREGATE_TYPE,
                    aggregate_id=registration.registration_id,
                    aggregate_version=registration.version,
                    occurred_at=registration.registered_at,
                    correlation_id=command.correlation_id,
                    causation_id=str(command.idempotency_key),
                    payload=registered_payload(registration),
                )
            )
            unit_of_work.add_idempotency(
                IdempotencyRecord(
                    actor_id=athlete_id,
                    key=command.idempotency_key,
                    operation=OPERATION,
                    request_fingerprint=fingerprint,
                    resource_id=registration.registration_id,
                    response=registration_document(view),
                    created_at=registration.registered_at,
                    expires_at=registration.registered_at + IDEMPOTENCY_TTL,
                )
            )
            unit_of_work.commit()
        return RegistrationResult(view)
