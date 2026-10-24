"""HU017: asociar y desasociar servicios médicos deportivos (ADR-038).

Autorización común (`authorize`): solo `athlete` con `services.sports_medicine.access`,
consultada ante Afiliación antes de abrir cualquier transacción local; sin la capacidad
`capability_required`, sin respuesta de Afiliación `upstream_unavailable`.

Asociar (`POST /v1/me/service-associations`, `Idempotency-Key` de 24 h por deportista):
1. Si la clave del deportista tiene un resultado vigente, se devuelve (CA3); con otro
   servicio u otra operación, `idempotency_key_reused`.
2. En una sola transacción: servicio inexistente, en borrador o de otra categoría →
   `service_not_found` (CA2); asociación activa existente → `association_exists` con su
   identificador (CA3); si no, la asociación `ACTIVE` y el resultado idempotente (CA1).
3. Dos solicitudes concurrentes del mismo deportista y servicio: el índice único parcial
   deja confirmar una sola; la otra recibe `association_exists` (o la respuesta original si
   compartían la clave).

Desasociar (`DELETE /v1/me/service-associations/{association_id}`): con la asociación
bloqueada, `ACTIVE` → `INACTIVE` con `ended_at`; repetirlo no cambia nada (CA5). Una
asociación inexistente o de otro deportista es `association_not_found`.

Sin eventos. Ningún dato de salud se lee, guarda ni comparte (CA4). Cada intento queda en
`audit.service_association_changed` (deportista, servicio, acción y resultado; la
correlación la agrega el registro).
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from complementary_services.application.errors import (
    ActiveAssociationConflict,
    AssociationExists,
    AssociationNotFound,
    CapabilitiesUnavailable,
    CapabilityRequired,
    IdempotencyKeyConflict,
    IdempotencyKeyReused,
    PermissionRequired,
    ServiceNotFound,
)
from complementary_services.application.ports import (
    AssociatedService,
    AssociationUnitOfWork,
    AssociationView,
    Caller,
    CapabilityChecker,
    Clock,
    IdempotencyRecord,
)
from complementary_services.domain.association.association import (
    ServiceAssociation,
    is_associable,
)

ATHLETE = "athlete"
MEDICAL_CAPABILITY = "services.sports_medicine.access"
OPERATION = "associate_service"
IDEMPOTENCY_TTL = timedelta(hours=24)
ASSOCIATE = "associate"
DISSOCIATE = "dissociate"

_audit = logging.getLogger("sportapp.audit")
_REJECTIONS = (
    PermissionRequired,
    CapabilityRequired,
    CapabilitiesUnavailable,
    ServiceNotFound,
    AssociationExists,
    AssociationNotFound,
    IdempotencyKeyReused,
)


@dataclass(frozen=True)
class AssociateServiceCommand:
    service_id: UUID
    idempotency_key: UUID


@dataclass(frozen=True)
class AssociatedResult:
    association: AssociationView
    replayed: bool = False


def request_fingerprint(athlete_id: UUID, service_id: UUID) -> str:
    """Huella de la operación, el deportista autenticado y el servicio."""
    canonical = json.dumps([OPERATION, str(athlete_id), str(service_id)])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def audit(caller: Caller, action: str, result: str, **fields: str) -> None:
    # Sin cuerpos, token ni datos de salud.
    _audit.info(
        "audit.service_association_changed",
        extra={
            "fields": {
                **fields,
                "athlete_id": str(caller.user_id),
                "action": action,
                "result": result,
            }
        },
    )


class MedicalAccess:
    """Actor y capacidad de las tres operaciones de HU017."""

    def __init__(self, capabilities: CapabilityChecker) -> None:
        self._capabilities = capabilities

    def authorize(self, caller: Caller) -> None:
        if caller.actor_type != ATHLETE:
            raise PermissionRequired()
        # Afiliación se consulta fuera de cualquier transacción local.
        if not self._capabilities.has_capability(caller.user_id, MEDICAL_CAPABILITY):
            raise CapabilityRequired()


def _rejection_code(error: Exception) -> str:
    return str(getattr(error, "code", type(error).__name__))


class AssociateService:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], AssociationUnitOfWork],
        access: MedicalAccess,
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._access = access
        self._clock = clock
        self._new_id = new_id

    def authorize(self, caller: Caller) -> None:
        """Actor y capacidad; el transporte lo llama antes de validar el cuerpo."""
        try:
            self._access.authorize(caller)
        except _REJECTIONS as error:
            audit(caller, ASSOCIATE, "rejected", reason=_rejection_code(error))
            raise

    def __call__(self, caller: Caller, command: AssociateServiceCommand) -> AssociatedResult:
        self.authorize(caller)
        return self.execute(caller, command)

    def execute(self, caller: Caller, command: AssociateServiceCommand) -> AssociatedResult:
        """El caso de uso con quien llama ya autorizado."""
        try:
            result = self._execute(caller.user_id, command)
        except _REJECTIONS as error:
            audit(
                caller,
                ASSOCIATE,
                "rejected",
                reason=_rejection_code(error),
                service_id=str(command.service_id),
            )
            raise
        audit(
            caller,
            ASSOCIATE,
            "replayed" if result.replayed else "associated",
            service_id=str(command.service_id),
            association_id=str(result.association.association.association_id),
        )
        return result

    def _execute(self, athlete_id: UUID, command: AssociateServiceCommand) -> AssociatedResult:
        fingerprint = request_fingerprint(athlete_id, command.service_id)
        previous = self._previous(athlete_id, command.idempotency_key, fingerprint)
        if previous is not None:
            return previous
        try:
            return self._associate(athlete_id, command, fingerprint)
        except IdempotencyKeyConflict:
            # Otra solicitud con la misma clave confirmó primero: su resultado.
            previous = self._previous(athlete_id, command.idempotency_key, fingerprint)
            if previous is None:
                raise
            return previous
        except ActiveAssociationConflict:
            # Otra solicitud confirmó primero la asociación activa; si era un reintento con
            # la misma clave, se devuelve su resultado.
            previous = self._previous(athlete_id, command.idempotency_key, fingerprint)
            if previous is not None:
                return previous
            with self._unit_of_work() as unit_of_work:
                existing = unit_of_work.active_association(athlete_id, command.service_id)
            if existing is None:
                raise
            raise AssociationExists(existing.association_id) from None

    def _previous(self, athlete_id: UUID, key: UUID, fingerprint: str) -> AssociatedResult | None:
        with self._unit_of_work() as unit_of_work:
            record = unit_of_work.find_idempotency(athlete_id, key)
        if record is None or record.expires_at <= self._clock.now():
            return None
        if record.operation != OPERATION or record.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused()
        return AssociatedResult(
            AssociationView.from_document(record.response, athlete_id), replayed=True
        )

    def _associate(
        self, athlete_id: UUID, command: AssociateServiceCommand, fingerprint: str
    ) -> AssociatedResult:
        now = self._clock.now()
        with self._unit_of_work() as unit_of_work:
            offer = unit_of_work.service(command.service_id)
            if offer is None or not is_associable(offer):
                raise ServiceNotFound()
            stale = unit_of_work.find_idempotency(athlete_id, command.idempotency_key)
            if stale is not None:
                if stale.expires_at > now:
                    raise IdempotencyKeyConflict()
                unit_of_work.delete_idempotency(athlete_id, command.idempotency_key)
            existing = unit_of_work.active_association(athlete_id, command.service_id)
            if existing is not None:
                raise AssociationExists(existing.association_id)
            association = ServiceAssociation.associate(
                association_id=self._new_id(), offer=offer, athlete_id=athlete_id, now=now
            )
            unit_of_work.add_association(association)
            view = AssociationView(association, AssociatedService.of(offer))
            unit_of_work.add_idempotency(
                IdempotencyRecord(
                    provider_id=athlete_id,
                    key=command.idempotency_key,
                    operation=OPERATION,
                    request_fingerprint=fingerprint,
                    resource_id=association.association_id,
                    response=view.to_document(),
                    created_at=now,
                    expires_at=now + IDEMPOTENCY_TTL,
                )
            )
            unit_of_work.commit()
        return AssociatedResult(view)


class DissociateService:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], AssociationUnitOfWork],
        access: MedicalAccess,
        clock: Clock,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._access = access
        self._clock = clock

    def __call__(self, caller: Caller, association_id: UUID) -> ServiceAssociation:
        try:
            self._access.authorize(caller)
            association, ended = self._dissociate(caller.user_id, association_id)
        except _REJECTIONS as error:
            audit(
                caller,
                DISSOCIATE,
                "rejected",
                reason=_rejection_code(error),
                association_id=str(association_id),
            )
            raise
        audit(
            caller,
            DISSOCIATE,
            "dissociated" if ended else "already_inactive",
            association_id=str(association_id),
            service_id=str(association.service_id),
        )
        return association

    def _dissociate(
        self, athlete_id: UUID, association_id: UUID
    ) -> tuple[ServiceAssociation, bool]:
        now = self._clock.now()
        with self._unit_of_work() as unit_of_work:
            association = unit_of_work.lock_association(association_id)
            if association is None or association.athlete_id != athlete_id:
                raise AssociationNotFound()
            if not association.is_active:
                return association, False
            ended = association.end(now)
            unit_of_work.save_end(ended)
            unit_of_work.commit()
        return ended, True
