"""HU040: reservar y cancelar sesiones virtuales con entrenadores (ADR-046).

Reservar (`POST /v1/me/appointments`, `Idempotency-Key` de 24 h por deportista):
1. Solo `athlete` con `services.virtual_sessions.schedule`, consultada ante Afiliación
   antes de abrir cualquier transacción local (`capability_required`, CA3; sin respuesta
   `upstream_unavailable`).
2. Si la clave del deportista tiene un resultado vigente, se devuelve (CA5); con otro
   servicio o intervalo, `idempotency_key_reused`.
3. En una sola transacción: servicio no reservable → `service_not_bookable` (CA3); el
   intervalo del servicio se bloquea (`FOR UPDATE`); si no existe o empieza en menos de
   1 h → `availability_not_bookable`; cita activa del deportista en el intervalo →
   `appointment_exists` con su identificador; `booked_count = capacity` →
   `availability_full` (CA2). Si no, la cita `BOOKED`, el contador, la clave y
   `sesion.programada.v1` se confirman juntos (CA1).
4. El bloqueo del intervalo serializa las reservas concurrentes: con capacidad 1 solo una
   se confirma y las demás reciben `availability_full` (CA2).

Cancelar (`DELETE /v1/me/appointments/{appointment_id}`, solo `athlete`): con el intervalo
y la cita bloqueados (en ese orden, el mismo de la reserva), `BOOKED` →
`CANCELLED_BY_ATHLETE` si faltan 24 h o más (si no, `cancellation_window_closed`, CA4); el
contador baja y se publica `sesion.cancelada.v1`. Repetirlo devuelve la cita sin cambios ni
evento. Una cita inexistente o ajena es `appointment_not_found`.

Sin pago, videollamada ni reprogramación: `meeting_url` siempre `null` (CA6). Cada intento
queda en `audit.appointment_changed` (deportista, servicio, intervalo, acción y resultado).
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from complementary_services.application.errors import (
    ActiveAppointmentConflict,
    AppointmentExists,
    AppointmentNotFound,
    AvailabilityFull,
    AvailabilityNotBookable,
    CancellationWindowClosed,
    CapabilitiesUnavailable,
    CapabilityRequired,
    IdempotencyKeyConflict,
    IdempotencyKeyReused,
    PermissionRequired,
    ServiceNotBookable,
)
from complementary_services.application.ports import (
    AppointmentUnitOfWork,
    AppointmentView,
    AssociatedService,
    Caller,
    CapabilityChecker,
    Clock,
    IdempotencyRecord,
    OutboxMessage,
)
from complementary_services.domain.appointments.appointment import (
    Appointment,
    is_bookable_service,
    is_bookable_slot,
)
from complementary_services.domain.catalog.publication import utc_instant

ATHLETE = "athlete"
VIRTUAL_SESSIONS_CAPABILITY = "services.virtual_sessions.schedule"
OPERATION = "book_appointment"
IDEMPOTENCY_TTL = timedelta(hours=24)
AGGREGATE_TYPE = "appointment"
BOOKED_EVENT = "sesion.programada.v1"
CANCELLED_EVENT = "sesion.cancelada.v1"
SCHEMA_VERSION = 1
BOOK = "book"
CANCEL = "cancel"

_audit = logging.getLogger("sportapp.audit")
_REJECTIONS = (
    PermissionRequired,
    CapabilityRequired,
    CapabilitiesUnavailable,
    ServiceNotBookable,
    AvailabilityNotBookable,
    AvailabilityFull,
    AppointmentExists,
    AppointmentNotFound,
    CancellationWindowClosed,
    IdempotencyKeyReused,
)


@dataclass(frozen=True)
class BookAppointmentCommand:
    service_id: UUID
    availability_id: UUID
    idempotency_key: UUID
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class BookedAppointment:
    appointment: AppointmentView
    replayed: bool = False


@dataclass(frozen=True)
class CancelledAppointment:
    appointment: AppointmentView
    cancelled: bool


def request_fingerprint(athlete_id: UUID, service_id: UUID, availability_id: UUID) -> str:
    """Huella de la operación, el deportista autenticado, el servicio y el intervalo."""
    canonical = json.dumps([OPERATION, str(athlete_id), str(service_id), str(availability_id)])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def appointment_payload(appointment: Appointment) -> dict[str, Any]:
    """Estado completo de la cita para `sesion.programada.v1` y `sesion.cancelada.v1`; sin
    datos de salud ni del perfil (ADR-046 §5)."""
    return {
        "appointment_id": str(appointment.appointment_id),
        "athlete_id": str(appointment.athlete_id),
        "provider_id": str(appointment.provider_id),
        "service_id": str(appointment.service_id),
        "availability_id": str(appointment.availability_id),
        "starts_at": utc_instant(appointment.starts_at),
        "ends_at": utc_instant(appointment.ends_at),
        "time_zone": appointment.time_zone,
        "status": appointment.status.value,
        "meeting_url": appointment.meeting_url,
        "booked_at": utc_instant(appointment.booked_at),
        "cancelled_at": (
            utc_instant(appointment.cancelled_at) if appointment.cancelled_at else None
        ),
    }


def audit(caller: Caller, action: str, result: str, **fields: str) -> None:
    # Sin cuerpos, token ni datos de salud.
    _audit.info(
        "audit.appointment_changed",
        extra={
            "fields": {
                **fields,
                "athlete_id": str(caller.user_id),
                "action": action,
                "result": result,
            }
        },
    )


def _rejection_code(error: Exception) -> str:
    return str(getattr(error, "code", type(error).__name__))


def authorize_athlete(caller: Caller) -> None:
    """Solo el deportista (cancelar y consultar no exigen la capacidad)."""
    if caller.actor_type != ATHLETE:
        raise PermissionRequired()


class VirtualSessionAccess:
    """Actor y capacidad de la reserva (ADR-046 §2)."""

    def __init__(self, capabilities: CapabilityChecker) -> None:
        self._capabilities = capabilities

    def authorize(self, caller: Caller) -> None:
        authorize_athlete(caller)
        # Afiliación se consulta fuera de cualquier transacción local.
        if not self._capabilities.has_capability(caller.user_id, VIRTUAL_SESSIONS_CAPABILITY):
            raise CapabilityRequired()


class BookAppointment:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], AppointmentUnitOfWork],
        access: VirtualSessionAccess,
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
            audit(caller, BOOK, "rejected", reason=_rejection_code(error))
            raise

    def __call__(self, caller: Caller, command: BookAppointmentCommand) -> BookedAppointment:
        self.authorize(caller)
        return self.execute(caller, command)

    def execute(self, caller: Caller, command: BookAppointmentCommand) -> BookedAppointment:
        """El caso de uso con quien llama ya autorizado."""
        fields = {
            "service_id": str(command.service_id),
            "availability_id": str(command.availability_id),
        }
        try:
            result = self._execute(caller.user_id, command)
        except _REJECTIONS as error:
            audit(caller, BOOK, "rejected", reason=_rejection_code(error), **fields)
            raise
        audit(
            caller,
            BOOK,
            "replayed" if result.replayed else "booked",
            appointment_id=str(result.appointment.appointment.appointment_id),
            **fields,
        )
        return result

    def _execute(self, athlete_id: UUID, command: BookAppointmentCommand) -> BookedAppointment:
        fingerprint = request_fingerprint(athlete_id, command.service_id, command.availability_id)
        previous = self._previous(athlete_id, command.idempotency_key, fingerprint)
        if previous is not None:
            return previous
        try:
            return self._book(athlete_id, command, fingerprint)
        except (AppointmentExists, IdempotencyKeyConflict):
            # Un reintento con la misma clave pudo confirmar primero: su resultado.
            previous = self._previous(athlete_id, command.idempotency_key, fingerprint)
            if previous is None:
                raise
            return previous
        except ActiveAppointmentConflict:
            # Defensa del índice parcial: otra solicitud confirmó primero la cita activa.
            previous = self._previous(athlete_id, command.idempotency_key, fingerprint)
            if previous is not None:
                return previous
            with self._unit_of_work() as unit_of_work:
                existing = unit_of_work.active_appointment(athlete_id, command.availability_id)
            if existing is None:
                raise
            raise AppointmentExists(existing.appointment_id) from None

    def _previous(self, athlete_id: UUID, key: UUID, fingerprint: str) -> BookedAppointment | None:
        with self._unit_of_work() as unit_of_work:
            record = unit_of_work.find_idempotency(athlete_id, key)
        if record is None or record.expires_at <= self._clock.now():
            return None
        if record.operation != OPERATION or record.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused()
        return BookedAppointment(AppointmentView.from_document(record.response), replayed=True)

    def _book(
        self, athlete_id: UUID, command: BookAppointmentCommand, fingerprint: str
    ) -> BookedAppointment:
        now = self._clock.now()
        with self._unit_of_work() as unit_of_work:
            offer = unit_of_work.service(command.service_id)
            if offer is None or not is_bookable_service(offer):
                raise ServiceNotBookable()
            slot = unit_of_work.lock_slot(command.service_id, command.availability_id)
            if slot is None or not is_bookable_slot(slot, now):
                raise AvailabilityNotBookable()
            # Con el intervalo bloqueado: un reintento concurrente con la misma clave que
            # confirmó primero se devuelve como resultado original.
            stale = unit_of_work.find_idempotency(athlete_id, command.idempotency_key)
            if stale is not None:
                if stale.expires_at > now:
                    raise IdempotencyKeyConflict()
                unit_of_work.delete_idempotency(athlete_id, command.idempotency_key)
            existing = unit_of_work.active_appointment(athlete_id, slot.availability_id)
            if existing is not None:
                raise AppointmentExists(existing.appointment_id)
            if not slot.has_room:
                raise AvailabilityFull()
            appointment = Appointment.book(
                appointment_id=self._new_id(),
                athlete_id=athlete_id,
                offer=offer,
                slot=slot,
                now=now,
            )
            # El contador va primero: su UPDATE sale enseguida y el INSERT de la cita queda para
            # el COMMIT, donde el índice parcial se traduce a `ActiveAppointmentConflict`.
            unit_of_work.save_booked_count(replace(slot, booked_count=slot.booked_count + 1))
            unit_of_work.add_appointment(appointment)
            unit_of_work.add_outbox(
                OutboxMessage(
                    event_id=self._new_id(),
                    event_type=BOOKED_EVENT,
                    schema_version=SCHEMA_VERSION,
                    aggregate_type=AGGREGATE_TYPE,
                    aggregate_id=appointment.appointment_id,
                    aggregate_version=appointment.version,
                    occurred_at=now,
                    correlation_id=command.correlation_id,
                    causation_id=str(command.idempotency_key),
                    payload=appointment_payload(appointment),
                )
            )
            view = AppointmentView(appointment, AssociatedService.of(offer))
            unit_of_work.add_idempotency(
                IdempotencyRecord(
                    provider_id=athlete_id,
                    key=command.idempotency_key,
                    operation=OPERATION,
                    request_fingerprint=fingerprint,
                    resource_id=appointment.appointment_id,
                    response=view.to_document(),
                    created_at=now,
                    expires_at=now + IDEMPOTENCY_TTL,
                )
            )
            unit_of_work.commit()
        return BookedAppointment(view)


class CancelAppointment:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], AppointmentUnitOfWork],
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._new_id = new_id

    def __call__(
        self, caller: Caller, appointment_id: UUID, correlation_id: UUID | None = None
    ) -> CancelledAppointment:
        try:
            authorize_athlete(caller)
            result = self._cancel(caller.user_id, appointment_id, correlation_id)
        except _REJECTIONS as error:
            audit(
                caller,
                CANCEL,
                "rejected",
                reason=_rejection_code(error),
                appointment_id=str(appointment_id),
            )
            raise
        appointment = result.appointment.appointment
        audit(
            caller,
            CANCEL,
            "cancelled" if result.cancelled else "already_cancelled",
            appointment_id=str(appointment_id),
            service_id=str(appointment.service_id),
            availability_id=str(appointment.availability_id),
        )
        return result

    def _cancel(
        self, athlete_id: UUID, appointment_id: UUID, correlation_id: UUID | None
    ) -> CancelledAppointment:
        now = self._clock.now()
        with self._unit_of_work() as unit_of_work:
            current = unit_of_work.appointment(appointment_id)
            if current is None or current.athlete_id != athlete_id:
                raise AppointmentNotFound()
            # El intervalo antes que la cita: el mismo orden de bloqueo que la reserva.
            slot = unit_of_work.lock_slot(current.service_id, current.availability_id)
            appointment = unit_of_work.lock_appointment(appointment_id)
            offer = unit_of_work.service(current.service_id)
            if slot is None or appointment is None or offer is None:
                raise AppointmentNotFound()
            service = AssociatedService.of(offer)
            if not appointment.is_booked:
                return CancelledAppointment(AppointmentView(appointment, service), False)
            if not appointment.can_cancel(now):
                raise CancellationWindowClosed()
            cancelled = appointment.cancel(now)
            unit_of_work.save_cancellation(cancelled)
            unit_of_work.save_booked_count(replace(slot, booked_count=slot.booked_count - 1))
            unit_of_work.add_outbox(
                OutboxMessage(
                    event_id=self._new_id(),
                    event_type=CANCELLED_EVENT,
                    schema_version=SCHEMA_VERSION,
                    aggregate_type=AGGREGATE_TYPE,
                    aggregate_id=cancelled.appointment_id,
                    aggregate_version=cancelled.version,
                    occurred_at=now,
                    correlation_id=correlation_id,
                    causation_id=None,
                    payload=appointment_payload(cancelled),
                )
            )
            unit_of_work.commit()
        return CancelledAppointment(AppointmentView(cancelled, service), True)
