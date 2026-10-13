"""HU015: crear un servicio en borrador (ADR-027).

Secuencia:
1. Verificar que quien llama tenga el permiso `services.create` (CA2).
2. Validar y normalizar la oferta sin tocar persistencia (CA3, CA6, CA7).
3. Si la clave de idempotencia del profesional ya tiene un resultado vigente,
   devolverlo; con otro payload, `idempotency_key_reused` (CA4).
4. En una sola transacción: servicio `DRAFT` versión 1, disponibilidad y resultado
   idempotente (CA1). Sin eventos: HU015 no publica nada; un borrador no aparece en
   ningún catálogo (CA5).
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
)
from complementary_services.application.ports import (
    Caller,
    Clock,
    CreationUnitOfWork,
    IdempotencyRecord,
    PermissionPolicy,
    ServiceView,
)
from complementary_services.domain.catalog.offer import (
    OfferInput,
    ServiceOffer,
    ValidOffer,
    validate_offer,
)
from complementary_services.domain.validation import InvalidServiceOffer

OPERATION = "create_service"
CREATE_PERMISSION = "services.create"
IDEMPOTENCY_TTL = timedelta(hours=24)

_audit = logging.getLogger("sportapp.audit")


@dataclass(frozen=True)
class CreateServiceCommand:
    offer: OfferInput
    idempotency_key: UUID
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class CreatedService:
    service: ServiceView
    replayed: bool = False


def request_fingerprint(provider_id: UUID, offer: ValidOffer) -> str:
    """Huella del payload normalizado y del profesional autenticado."""
    canonical = json.dumps(
        [
            OPERATION,
            str(provider_id),
            offer.name,
            offer.description,
            offer.category.value,
            offer.modality.value,
            str(offer.price.amount),
            offer.price.currency.value,
            offer.price.billing_unit.value,
            [
                [
                    interval.starts_at.isoformat(),
                    interval.ends_at.isoformat(),
                    interval.time_zone,
                    interval.capacity,
                ]
                for interval in offer.availability
            ],
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CreateService:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], CreationUnitOfWork],
        permissions: PermissionPolicy,
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._permissions = permissions
        self._clock = clock
        self._new_id = new_id

    def __call__(self, caller: Caller, command: CreateServiceCommand) -> CreatedService:
        try:
            result = self._execute(caller, command)
        except (PermissionRequired, InvalidServiceOffer, IdempotencyKeyReused) as error:
            _audit.info(
                "audit.service_creation",
                extra={
                    "fields": {
                        "result": "rejected",
                        "reason": error.code,
                        "provider_id": str(caller.user_id),
                    }
                },
            )
            raise
        _audit.info(
            "audit.service_creation",
            extra={
                "fields": {
                    "result": "replayed" if result.replayed else "created",
                    "service_id": str(result.service.offer.service_id),
                    "provider_id": str(caller.user_id),
                }
            },
        )
        return result

    def validate(self, caller: Caller, offer: OfferInput) -> ValidOffer:
        """Permiso y reglas de la oferta, sin persistir; también para informar todos los
        errores cuando la solicitud ya es inválida por otra razón (p. ej., la clave)."""
        if not self._permissions.allows(caller.actor_type, CREATE_PERMISSION):
            raise PermissionRequired()
        return validate_offer(offer, self._clock.now())

    def _execute(self, caller: Caller, command: CreateServiceCommand) -> CreatedService:
        offer = self.validate(caller, command.offer)
        now = self._clock.now()
        fingerprint = request_fingerprint(caller.user_id, offer)

        previous = self._previous(caller.user_id, command.idempotency_key, fingerprint)
        if previous is not None:
            return previous
        draft = ServiceOffer.draft(
            service_id=self._new_id(), provider_id=caller.user_id, offer=offer, now=now
        )
        try:
            return self._create(command, draft, fingerprint)
        except IdempotencyKeyConflict:
            # Una solicitud concurrente con la misma clave confirmó primero: su resultado.
            previous = self._previous(caller.user_id, command.idempotency_key, fingerprint)
            if previous is None:
                raise
            return previous

    def _previous(self, provider_id: UUID, key: UUID, fingerprint: str) -> CreatedService | None:
        with self._unit_of_work() as unit_of_work:
            record = unit_of_work.find_idempotency(provider_id, key)
        if record is None or record.expires_at <= self._clock.now():
            return None
        if record.operation != OPERATION or record.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused()
        return CreatedService(ServiceView.from_document(record.response), replayed=True)

    def _create(
        self, command: CreateServiceCommand, draft: ServiceOffer, fingerprint: str
    ) -> CreatedService:
        view = ServiceView(draft)
        now = draft.created_at
        with self._unit_of_work() as unit_of_work:
            existing = unit_of_work.find_idempotency(draft.provider_id, command.idempotency_key)
            if existing is not None:
                if existing.expires_at > now:
                    raise IdempotencyKeyConflict()
                unit_of_work.delete_idempotency(draft.provider_id, command.idempotency_key)
            unit_of_work.add_service(draft)
            unit_of_work.add_idempotency(
                IdempotencyRecord(
                    provider_id=draft.provider_id,
                    key=command.idempotency_key,
                    operation=OPERATION,
                    request_fingerprint=fingerprint,
                    resource_id=draft.service_id,
                    response=view.to_document(),
                    created_at=now,
                    expires_at=now + IDEMPOTENCY_TTL,
                )
            )
            unit_of_work.commit()
        return CreatedService(view)
