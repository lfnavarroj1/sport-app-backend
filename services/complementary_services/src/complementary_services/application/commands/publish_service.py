"""HU019: publicar un servicio propio en borrador (ADR-027, «Contrato de publicación»).

Secuencia:
1. Verificar que quien llama tenga el permiso `services.publish`.
2. Si la clave de idempotencia del profesional ya tiene un resultado vigente, devolverlo;
   para otro servicio u otra operación, `idempotency_key_reused` (CA4).
3. En una sola transacción, con la fila del servicio bloqueada (`FOR UPDATE`) para
   serializar solicitudes concurrentes:
   - inexistente o de otro profesional → `service_not_found` (CA3);
   - la clave se revisa de nuevo después del bloqueo: si otra solicitud con la misma clave
     confirmó primero, se devuelve su respuesta;
   - ya `PUBLISHED` → estado vigente, sin subir la versión ni emitir otro evento (CA4);
   - `DRAFT` → reglas de publicación (CA1, CA2), transición, `servicio.publicado.v1` en la
     outbox y resultado idempotente, confirmados juntos (CA5).
La respuesta se emite después del COMMIT; el evento lo envía `servicios-outbox-publisher`.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from complementary_services.application.errors import (
    IdempotencyKeyConflict,
    IdempotencyKeyReused,
    PermissionRequired,
    ServiceNotFound,
)
from complementary_services.application.ports import (
    Caller,
    Clock,
    IdempotencyRecord,
    OutboxMessage,
    PermissionPolicy,
    PublicationUnitOfWork,
    PublicationView,
)
from complementary_services.domain.catalog.offer import ServiceStatus
from complementary_services.domain.catalog.publication import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    published_payload,
)
from complementary_services.domain.validation import InvalidServiceOffer

OPERATION = "publish_service"
PUBLISH_PERMISSION = "services.publish"
IDEMPOTENCY_TTL = timedelta(hours=24)

_audit = logging.getLogger("sportapp.audit")
_REJECTIONS = (PermissionRequired, ServiceNotFound, InvalidServiceOffer, IdempotencyKeyReused)


@dataclass(frozen=True)
class PublishServiceCommand:
    service_id: UUID
    idempotency_key: UUID
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class PublishedService:
    publication: PublicationView
    replayed: bool = False
    # `False` cuando el servicio ya estaba publicado: no hubo transición ni evento.
    transitioned: bool = True


def request_fingerprint(provider_id: UUID, service_id: UUID) -> str:
    """Huella de la operación, el profesional autenticado y el servicio."""
    canonical = json.dumps([OPERATION, str(provider_id), str(service_id)])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PublishService:
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

    def __call__(self, caller: Caller, command: PublishServiceCommand) -> PublishedService:
        try:
            result = self._execute(caller, command)
        except _REJECTIONS as error:
            self._audit(caller, command, result="rejected", reason=error.code)
            raise
        if result.replayed:
            outcome = "replayed"
        elif result.transitioned:
            outcome = "published"
        else:
            outcome = "already_published"
        self._audit(caller, command, result=outcome)
        return result

    @staticmethod
    def _audit(caller: Caller, command: PublishServiceCommand, **fields: str) -> None:
        # Sin cuerpos ni token.
        _audit.info(
            "audit.service_publication",
            extra={
                "fields": {
                    **fields,
                    "service_id": str(command.service_id),
                    "provider_id": str(caller.user_id),
                }
            },
        )

    def authorize(self, caller: Caller) -> None:
        """Permiso de la operación; también antes de validar la clave en el transporte."""
        if not self._permissions.allows(caller.actor_type, PUBLISH_PERMISSION):
            raise PermissionRequired()

    def _execute(self, caller: Caller, command: PublishServiceCommand) -> PublishedService:
        self.authorize(caller)
        fingerprint = request_fingerprint(caller.user_id, command.service_id)
        previous = self._previous(caller.user_id, command.idempotency_key, fingerprint)
        if previous is not None:
            return previous
        try:
            return self._publish(caller.user_id, command, fingerprint)
        except IdempotencyKeyConflict:
            # Otra solicitud con la misma clave confirmó primero: su resultado.
            previous = self._previous(caller.user_id, command.idempotency_key, fingerprint)
            if previous is None:
                raise
            return previous

    def _previous(self, provider_id: UUID, key: UUID, fingerprint: str) -> PublishedService | None:
        with self._unit_of_work() as unit_of_work:
            record = unit_of_work.find_idempotency(provider_id, key)
        if record is None or record.expires_at <= self._clock.now():
            return None
        if record.operation != OPERATION or record.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused()
        return PublishedService(PublicationView.from_document(record.response), replayed=True)

    def _publish(
        self, provider_id: UUID, command: PublishServiceCommand, fingerprint: str
    ) -> PublishedService:
        now = self._clock.now()
        with self._unit_of_work() as unit_of_work:
            offer = unit_of_work.lock_service(command.service_id)
            if offer is None or offer.provider_id != provider_id:
                raise ServiceNotFound()
            # Con el servicio ya bloqueado: un reintento concurrente con la misma clave que
            # confirmó primero se devuelve como resultado original.
            stale = unit_of_work.find_idempotency(provider_id, command.idempotency_key)
            if stale is not None:
                if stale.expires_at > now:
                    raise IdempotencyKeyConflict()
                unit_of_work.delete_idempotency(provider_id, command.idempotency_key)
            transitioned = offer.status is ServiceStatus.DRAFT
            if transitioned:
                offer = offer.publish(now)
                unit_of_work.save_publication(offer)
                unit_of_work.add_outbox(
                    OutboxMessage(
                        event_id=self._new_id(),
                        event_type=EVENT_TYPE,
                        schema_version=SCHEMA_VERSION,
                        aggregate_type=AGGREGATE_TYPE,
                        aggregate_id=offer.service_id,
                        aggregate_version=offer.version,
                        occurred_at=now,
                        correlation_id=command.correlation_id,
                        causation_id=str(command.idempotency_key),
                        payload=published_payload(offer),
                    )
                )
            view = PublicationView.of(offer)
            unit_of_work.add_idempotency(
                IdempotencyRecord(
                    provider_id=provider_id,
                    key=command.idempotency_key,
                    operation=OPERATION,
                    request_fingerprint=fingerprint,
                    resource_id=offer.service_id,
                    response=view.to_document(),
                    created_at=now,
                    expires_at=now + IDEMPOTENCY_TTL,
                )
            )
            unit_of_work.commit()
        return PublishedService(view, transitioned=transitioned)
