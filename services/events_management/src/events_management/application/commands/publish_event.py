"""HU024: publicar un evento masivo propio (ADR-033).

Secuencia:
1. Verificar que quien llama sea `organizer` con el permiso `events.publish` (CA6).
2. Si la clave de idempotencia del organizador ya tiene un resultado vigente para este
   evento, devolverlo; con otro evento, `idempotency_key_reused` (CA4).
3. En una sola transacción de escritura, con la fila del evento bloqueada:
   - inexistente o de otro organizador → `event_not_found`, sin cambios (CA3);
   - ya `PUBLISHED` → su estado vigente, sin nueva versión ni evento (CA4);
   - `DRAFT` → revalidar ADR-028 e inicio futuro (`422`, CA2), `PUBLISHED` con versión + 1
     y `published_at`, `evento.publicado.v1` en la outbox y el resultado idempotente
     (CA1, CA5).
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
    IdempotencyKeyConflict,
    IdempotencyKeyReused,
    PermissionRequired,
)
from events_management.application.messages import parse_published_event_document
from events_management.application.ports import (
    Caller,
    Clock,
    IdempotencyRecord,
    OutboxMessage,
    PermissionPolicy,
    PublicationUnitOfWork,
)
from events_management.domain.sport_events.model import (
    EventNotFound,
    EventStatus,
    SportEvent,
)
from events_management.domain.sport_events.published import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    aggregate_document,
)
from events_management.domain.validation import InvalidSportEvent

OPERATION = "publish_event"
ORGANIZER = "organizer"
PUBLISH_PERMISSION = "events.publish"
IDEMPOTENCY_TTL = timedelta(hours=24)

_audit = logging.getLogger("sportapp.audit")
_REJECTIONS = (PermissionRequired, EventNotFound, InvalidSportEvent, IdempotencyKeyReused)


@dataclass(frozen=True)
class PublishEventCommand:
    event_id: UUID
    idempotency_key: UUID
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class PublicationResult:
    event: SportEvent
    replayed: bool = False
    already_published: bool = False


def request_fingerprint(organizer_id: UUID, event_id: UUID) -> str:
    """Huella de organizador + operación + evento (ADR-033): la misma clave con otro evento
    nunca comparte resultado."""
    canonical = json.dumps([OPERATION, str(organizer_id), str(event_id)])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PublishEvent:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], PublicationUnitOfWork],
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
            caller.actor_type, PUBLISH_PERMISSION
        ):
            raise PermissionRequired()

    def __call__(self, caller: Caller, command: PublishEventCommand) -> PublicationResult:
        try:
            result = self._execute(caller, command)
        except _REJECTIONS as error:
            self._audit(caller, command, result="rejected", reason=error.code)
            raise
        if result.replayed:
            outcome = "replayed"
        elif result.already_published:
            outcome = "already_published"
        else:
            outcome = "published"
        self._audit(caller, command, result=outcome)
        return result

    @staticmethod
    def _audit(caller: Caller, command: PublishEventCommand, **fields: str) -> None:
        # Sin textos libres, ubicación ni token (ADR-033, datos y seguridad).
        _audit.info(
            "audit.event_published",
            extra={
                "fields": {
                    **fields,
                    "event_id": str(command.event_id),
                    "organizer_id": str(caller.user_id),
                    "correlation_id": str(command.correlation_id)
                    if command.correlation_id
                    else None,
                }
            },
        )

    def _execute(self, caller: Caller, command: PublishEventCommand) -> PublicationResult:
        self.authorize(caller)
        fingerprint = request_fingerprint(caller.user_id, command.event_id)
        previous = self._previous(caller.user_id, command.idempotency_key, fingerprint)
        if previous is not None:
            return previous
        try:
            return self._publish(caller.user_id, command, fingerprint)
        except IdempotencyKeyConflict:
            # Un reintento concurrente con la misma clave confirmó primero: su resultado.
            previous = self._previous(caller.user_id, command.idempotency_key, fingerprint)
            if previous is None:
                raise
            return previous

    def _previous(
        self, organizer_id: UUID, key: UUID, fingerprint: str
    ) -> PublicationResult | None:
        with self._unit_of_work() as unit_of_work:
            record = unit_of_work.find_idempotency(organizer_id, OPERATION, key)
        if record is None or record.expires_at <= self._clock.now():
            return None
        if record.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused()
        return PublicationResult(parse_published_event_document(record.response), replayed=True)

    def _publish(
        self, organizer_id: UUID, command: PublishEventCommand, fingerprint: str
    ) -> PublicationResult:
        now = self._clock.now()
        with self._unit_of_work() as unit_of_work:
            event = unit_of_work.lock_event(command.event_id)
            if event is None or event.organizer_id != organizer_id:
                raise EventNotFound()
            # Con el evento ya bloqueado: un reintento concurrente con la misma clave que
            # confirmó primero se devuelve como resultado original (CA4).
            stale = unit_of_work.find_idempotency(organizer_id, OPERATION, command.idempotency_key)
            if stale is not None:
                if stale.expires_at > now:
                    raise IdempotencyKeyConflict()
                unit_of_work.delete_idempotency(organizer_id, OPERATION, command.idempotency_key)
            already_published = event.status is EventStatus.PUBLISHED
            if not already_published:
                event = event.publish(now)
                unit_of_work.update_event(event)
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
                        causation_id=str(command.idempotency_key),
                        payload=aggregate_document(event),
                    )
                )
            # También con «ya publicado»: repetir esta clave devuelve la misma respuesta.
            unit_of_work.add_idempotency(
                IdempotencyRecord(
                    actor_id=organizer_id,
                    key=command.idempotency_key,
                    operation=OPERATION,
                    request_fingerprint=fingerprint,
                    resource_id=event.event_id,
                    response=aggregate_document(event),
                    created_at=now,
                    expires_at=now + IDEMPOTENCY_TTL,
                )
            )
            unit_of_work.commit()
        return PublicationResult(event, already_published=already_published)
