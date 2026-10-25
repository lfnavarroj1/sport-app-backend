"""HU040: sesiones virtuales con entrenadores (ADR-046) sin infraestructura.

Trazabilidad: CA1 `201` con la cita `BOOKED` y `sesion.programada.v1` en la misma
confirmación; CA2 `409 availability_full` y `409 appointment_exists`; CA3
`403 capability_required` y `422 service_not_bookable`; CA4 cancelación con 24 h,
`CANCELLED_BY_ATHLETE`, cupo recuperado, `sesion.cancelada.v1` y
`422 cancellation_window_closed`; CA5 la misma clave devuelve la respuesta original; CA6
`meeting_url` nulo y la cita sigue consultable (también cancelada) por deportista y
profesional.
"""

import logging
import unittest
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

import test_create_service as hu015
from fakes import NOW, FixedClock
from fastapi.testclient import TestClient
from test_service_availability import FakeCapabilities, offer

from complementary_services.api.appointments import build_appointments_router, parse_booking
from complementary_services.application.commands.appointments import (
    BOOKED_EVENT,
    CANCELLED_EVENT,
    VIRTUAL_SESSIONS_CAPABILITY,
    BookAppointment,
    BookAppointmentCommand,
    CancelAppointment,
    VirtualSessionAccess,
    appointment_payload,
)
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
    AppointmentView,
    AssociatedService,
    Caller,
    IdempotencyRecord,
    OutboxMessage,
    OwnAppointmentsQuery,
)
from complementary_services.application.queries.my_appointments import (
    ListMyAppointments,
    MyAppointmentsFilters,
    decode_cursor,
)
from complementary_services.application.queries.my_services import InvalidServicesQuery
from complementary_services.domain.appointments.appointment import (
    Appointment,
    AppointmentStatus,
    BookableSlot,
    is_bookable_service,
    is_bookable_slot,
)
from complementary_services.domain.catalog.offer import (
    Category,
    Modality,
    ServiceOffer,
    ServiceStatus,
)
from complementary_services.domain.validation import Issue, Violation
from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings

SCHEDULE = frozenset({VIRTUAL_SESSIONS_CAPABILITY})


def slot_for(
    service: ServiceOffer,
    *,
    starts_at: datetime | None = None,
    capacity: int = 1,
    booked_count: int = 0,
) -> BookableSlot:
    start = starts_at or NOW + timedelta(days=3)
    return BookableSlot(
        availability_id=uuid4(),
        service_id=service.service_id,
        starts_at=start,
        ends_at=start + timedelta(hours=1),
        time_zone="America/Bogota",
        capacity=capacity,
        booked_count=booked_count,
    )


@dataclass
class AppointmentStore:
    services: dict[UUID, ServiceOffer] = field(default_factory=dict)
    slots: dict[UUID, BookableSlot] = field(default_factory=dict)
    appointments: dict[UUID, Appointment] = field(default_factory=dict)
    idempotency: dict[tuple[UUID, UUID], IdempotencyRecord] = field(default_factory=dict)
    outbox: list[OutboxMessage] = field(default_factory=list)


class InMemoryAppointmentUnitOfWork:
    """Aplica los cambios solo al confirmar y valida la misma unicidad que PostgreSQL.

    Las pruebas son secuenciales: los bloqueos no se simulan (la integración los prueba).
    """

    def __init__(self, store: AppointmentStore, before_commit: Any = None) -> None:
        self.store = store
        self.before_commit = before_commit
        self._appointments: list[Appointment] = []
        self._slots: list[BookableSlot] = []
        self._records: list[IdempotencyRecord] = []
        self._deleted: list[tuple[UUID, UUID]] = []
        self._outbox: list[OutboxMessage] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._appointments, self._slots, self._records = [], [], []
        self._deleted, self._outbox = [], []

    def find_idempotency(self, provider_id: UUID, key: UUID) -> IdempotencyRecord | None:
        return self.store.idempotency.get((provider_id, key))

    def delete_idempotency(self, provider_id: UUID, key: UUID) -> None:
        self._deleted.append((provider_id, key))

    def add_idempotency(self, record: IdempotencyRecord) -> None:
        self._records.append(record)

    def service(self, service_id: UUID) -> ServiceOffer | None:
        return self.store.services.get(service_id)

    def lock_slot(self, service_id: UUID, availability_id: UUID) -> BookableSlot | None:
        slot = self.store.slots.get(availability_id)
        return slot if slot is not None and slot.service_id == service_id else None

    def save_booked_count(self, slot: BookableSlot) -> None:
        self._slots.append(slot)

    def active_appointment(self, athlete_id: UUID, availability_id: UUID) -> Appointment | None:
        return next(
            (
                item
                for item in self.store.appointments.values()
                if item.athlete_id == athlete_id
                and item.availability_id == availability_id
                and item.is_booked
            ),
            None,
        )

    def appointment(self, appointment_id: UUID) -> Appointment | None:
        return self.store.appointments.get(appointment_id)

    def lock_appointment(self, appointment_id: UUID) -> Appointment | None:
        return self.store.appointments.get(appointment_id)

    def add_appointment(self, appointment: Appointment) -> None:
        self._appointments.append(appointment)

    def save_cancellation(self, appointment: Appointment) -> None:
        self._appointments.append(appointment)

    def add_outbox(self, message: OutboxMessage) -> None:
        self._outbox.append(message)

    def commit(self) -> None:
        # `before_commit` entrega, una sola vez, lo que otra solicitud confirma antes.
        hook = self.before_commit() if self.before_commit is not None else None
        if hook is not None:
            hook()
        for key in self._deleted:
            self.store.idempotency.pop(key, None)
        if any((r.provider_id, r.key) in self.store.idempotency for r in self._records):
            raise IdempotencyKeyConflict()
        for item in self._appointments:
            if item.is_booked and any(
                other.appointment_id != item.appointment_id
                and other.is_booked
                and (other.athlete_id, other.availability_id)
                == (item.athlete_id, item.availability_id)
                for other in self.store.appointments.values()
            ):
                raise ActiveAppointmentConflict()
        for slot in self._slots:
            if not 0 <= slot.booked_count <= slot.capacity:
                raise RuntimeError("booked_count fuera de rango")
            self.store.slots[slot.availability_id] = slot
        for item in self._appointments:
            self.store.appointments[item.appointment_id] = item
        for record in self._records:
            self.store.idempotency[(record.provider_id, record.key)] = record
        self.store.outbox.extend(self._outbox)


class InMemoryAppointmentReader:
    def __init__(self, store: AppointmentStore) -> None:
        self.store = store
        self.queries: list[OwnAppointmentsQuery] = []

    def list_own(self, query: OwnAppointmentsQuery) -> list[AppointmentView]:
        self.queries.append(query)
        items = sorted(
            (
                item
                for item in self.store.appointments.values()
                if (query.athlete_id is None or item.athlete_id == query.athlete_id)
                and (query.provider_id is None or item.provider_id == query.provider_id)
                and (query.status is None or item.status is query.status)
                and (query.starts_from is None or item.starts_at >= query.starts_from)
                and (query.starts_before is None or item.starts_at < query.starts_before)
                and (
                    query.after is None
                    or (item.starts_at, str(item.appointment_id))
                    > (query.after.starts_at, str(query.after.appointment_id))
                )
            ),
            key=lambda item: (item.starts_at, str(item.appointment_id)),
        )
        return [
            AppointmentView(item, AssociatedService.of(self.store.services[item.service_id]))
            for item in items[: query.limit]
        ]


class AppointmentCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = AppointmentStore()
        self.clock = FixedClock()
        self.capabilities = FakeCapabilities(SCHEDULE)
        self.service = self.add_service(offer("Entrenamiento funcional virtual"))
        self.slot = self.add_slot(slot_for(self.service))
        self.athlete = Caller(uuid4(), "athlete")
        self.hook: Any = None
        self.book = BookAppointment(
            unit_of_work=self.unit_of_work,
            access=VirtualSessionAccess(self.capabilities),
            clock=self.clock,
        )
        self.cancel = CancelAppointment(unit_of_work=self.unit_of_work, clock=self.clock)

    def unit_of_work(self) -> InMemoryAppointmentUnitOfWork:
        return InMemoryAppointmentUnitOfWork(self.store, self.take_hook)

    def take_hook(self) -> Any:
        hook, self.hook = self.hook, None
        return hook

    def add_service(self, service: ServiceOffer) -> ServiceOffer:
        self.store.services[service.service_id] = service
        return service

    def add_slot(self, slot: BookableSlot) -> BookableSlot:
        self.store.slots[slot.availability_id] = slot
        return slot

    def command(
        self, slot: BookableSlot | None = None, key: UUID | None = None
    ) -> BookAppointmentCommand:
        slot = slot or self.slot
        return BookAppointmentCommand(
            service_id=slot.service_id,
            availability_id=slot.availability_id,
            idempotency_key=key or uuid4(),
        )


class DomainRulesTest(unittest.TestCase):
    def test_only_published_virtual_coaching_is_bookable(self) -> None:
        """CA3: categoría, modalidad y estado."""
        self.assertTrue(is_bookable_service(offer("Virtual")))
        self.assertTrue(is_bookable_service(offer("Híbrido", modality=Modality.HYBRID)))
        self.assertFalse(is_bookable_service(offer("Presencial", modality=Modality.IN_PERSON)))
        self.assertFalse(is_bookable_service(offer("Nutrición", category=Category.NUTRITION)))
        self.assertFalse(is_bookable_service(offer("Borrador", status=ServiceStatus.DRAFT)))
        self.assertFalse(is_bookable_service(None))

    def test_booking_requires_one_hour_lead_time(self) -> None:
        service = offer("Virtual")
        exact = slot_for(service, starts_at=NOW + timedelta(hours=1))
        late = slot_for(service, starts_at=NOW + timedelta(minutes=59))

        self.assertTrue(is_bookable_slot(exact, NOW))
        self.assertFalse(is_bookable_slot(late, NOW))

    def test_book_copies_interval_and_has_no_meeting_url(self) -> None:
        """CA1 y CA6."""
        service = offer("Virtual")
        slot = slot_for(service, capacity=2, booked_count=1)

        appointment = Appointment.book(
            appointment_id=uuid4(), athlete_id=uuid4(), offer=service, slot=slot, now=NOW
        )

        self.assertEqual(
            (appointment.starts_at, appointment.ends_at, appointment.time_zone),
            (slot.starts_at, slot.ends_at, slot.time_zone),
        )
        self.assertEqual(appointment.provider_id, service.provider_id)
        self.assertEqual((appointment.status, appointment.version), (AppointmentStatus.BOOKED, 1))
        self.assertIsNone(appointment.meeting_url)

    def test_book_rejects_full_or_foreign_slot(self) -> None:
        service = offer("Virtual")
        for slot in (slot_for(service, booked_count=1), slot_for(offer("Otro"))):
            with self.subTest(slot=slot), self.assertRaises(ValueError):
                Appointment.book(
                    appointment_id=uuid4(), athlete_id=uuid4(), offer=service, slot=slot, now=NOW
                )

    def test_cancellation_window_is_24_hours(self) -> None:
        """CA4: con 24 h exactas se cancela; con menos no."""
        service = offer("Virtual")
        appointment = Appointment.book(
            appointment_id=uuid4(),
            athlete_id=uuid4(),
            offer=service,
            slot=slot_for(service, starts_at=NOW + timedelta(days=2)),
            now=NOW,
        )
        deadline = appointment.starts_at - timedelta(hours=24)

        cancelled = appointment.cancel(deadline)

        self.assertEqual(cancelled.status, AppointmentStatus.CANCELLED_BY_ATHLETE)
        self.assertEqual((cancelled.cancelled_at, cancelled.version), (deadline, 2))
        self.assertIs(cancelled.cancel(deadline + timedelta(hours=1)), cancelled)
        self.assertFalse(appointment.can_cancel(deadline + timedelta(seconds=1)))
        with self.assertRaises(ValueError):
            appointment.cancel(deadline + timedelta(seconds=1))


class BookAppointmentTest(AppointmentCase):
    def test_booking_commits_appointment_counter_key_and_event_together(self) -> None:
        """CA1: cita `BOOKED`, cupo ocupado, clave y `sesion.programada.v1` juntos."""
        key = uuid4()

        result = self.book(self.athlete, self.command(key=key))

        appointment = result.appointment.appointment
        self.assertFalse(result.replayed)
        self.assertEqual(self.store.appointments, {appointment.appointment_id: appointment})
        self.assertEqual(self.store.slots[self.slot.availability_id].booked_count, 1)
        self.assertIn((self.athlete.user_id, key), self.store.idempotency)
        [event] = self.store.outbox
        self.assertEqual(
            (event.event_type, event.aggregate_type, event.aggregate_id, event.aggregate_version),
            (BOOKED_EVENT, "appointment", appointment.appointment_id, 1),
        )
        self.assertEqual(event.causation_id, str(key))
        self.assertEqual(event.payload, appointment_payload(appointment))
        self.assertEqual(event.payload["status"], "BOOKED")
        self.assertIsNone(event.payload["meeting_url"])

    def test_same_key_returns_original_response(self) -> None:
        """CA5: sin segunda cita, contador ni evento."""
        command = self.command()

        first = self.book(self.athlete, command)
        second = self.book(self.athlete, command)

        self.assertTrue(second.replayed)
        self.assertEqual(second.appointment.to_document(), first.appointment.to_document())
        self.assertEqual(len(self.store.appointments), 1)
        self.assertEqual(len(self.store.outbox), 1)

    def test_same_key_with_other_interval_is_rejected(self) -> None:
        other = self.add_slot(slot_for(self.service, starts_at=NOW + timedelta(days=4)))
        key = uuid4()
        self.book(self.athlete, self.command(key=key))

        with self.assertRaises(IdempotencyKeyReused):
            self.book(self.athlete, self.command(other, key))

    def test_expired_key_is_reused_for_a_new_booking(self) -> None:
        key = uuid4()
        self.book(self.athlete, self.command(key=key))
        self.clock.advance(timedelta(hours=24))
        other = self.add_slot(slot_for(self.service, starts_at=NOW + timedelta(days=5)))

        result = self.book(self.athlete, self.command(other, key))

        self.assertFalse(result.replayed)
        self.assertEqual(len(self.store.appointments), 2)

    def test_full_interval(self) -> None:
        """CA2: con la capacidad ocupada, `availability_full`."""
        self.book(self.athlete, self.command())

        with self.assertRaises(AvailabilityFull):
            self.book(Caller(uuid4(), "athlete"), self.command())
        self.assertEqual(self.store.slots[self.slot.availability_id].booked_count, 1)

    def test_second_appointment_of_same_athlete(self) -> None:
        """CA2: `appointment_exists` con el identificador de la activa, aun con cupo."""
        slot = self.add_slot(slot_for(self.service, capacity=3))
        first = self.book(self.athlete, self.command(slot))

        with self.assertRaises(AppointmentExists) as raised:
            self.book(self.athlete, self.command(slot))
        self.assertEqual(
            raised.exception.appointment_id, first.appointment.appointment.appointment_id
        )

    def test_concurrent_retry_with_same_key_returns_its_result(self) -> None:
        command = self.command()

        def concurrent() -> None:
            # Otra solicitud con la misma clave confirma justo antes que esta.
            self.book(self.athlete, command)

        self.hook = concurrent
        result = self.book(self.athlete, command)

        self.assertTrue(result.replayed)
        self.assertEqual(len(self.store.appointments), 1)

    def test_concurrent_booking_with_other_key_is_appointment_exists(self) -> None:
        slot = self.add_slot(slot_for(self.service, capacity=2))

        def concurrent() -> None:
            self.book(self.athlete, self.command(slot))

        self.hook = concurrent
        with self.assertRaises(AppointmentExists):
            self.book(self.athlete, self.command(slot))
        self.assertEqual(len(self.store.appointments), 1)

    def test_ineligible_services(self) -> None:
        """CA3: categoría, modalidad, estado o inexistente → `service_not_bookable`."""
        services = [
            offer("Presencial", modality=Modality.IN_PERSON),
            offer("Médico", category=Category.SPORTS_MEDICINE),
            offer("Borrador", status=ServiceStatus.DRAFT),
        ]
        for service in services:
            self.add_service(service)
            slot = self.add_slot(slot_for(service))
            with self.subTest(service=service.name), self.assertRaises(ServiceNotBookable):
                self.book(self.athlete, self.command(slot))
        missing = replace(self.slot, service_id=uuid4())
        with self.assertRaises(ServiceNotBookable):
            self.book(self.athlete, self.command(missing))
        self.assertEqual(self.store.outbox, [])

    def test_interval_not_bookable(self) -> None:
        """Intervalo ajeno, inexistente o con menos de 1 h de antelación."""
        soon = self.add_slot(slot_for(self.service, starts_at=NOW + timedelta(minutes=30)))
        foreign = self.add_slot(slot_for(self.add_service(offer("Otro"))))
        cases = {
            "soon": self.command(soon),
            "foreign": replace(self.command(foreign), service_id=self.service.service_id),
            "missing": replace(self.command(), availability_id=uuid4()),
        }
        for name, command in cases.items():
            with self.subTest(name), self.assertRaises(AvailabilityNotBookable):
                self.book(self.athlete, command)

    def test_capability_and_actor(self) -> None:
        """CA3: sin la capacidad `capability_required`; otro actor `permission_required`;
        Afiliación caída `upstream_unavailable`, sin abrir transacciones."""
        provider = Caller(uuid4(), "service_provider")
        with self.assertRaises(PermissionRequired):
            self.book(provider, self.command())
        self.capabilities.granted = frozenset()
        with self.assertRaises(CapabilityRequired):
            self.book(self.athlete, self.command())
        self.capabilities.failure = CapabilitiesUnavailable
        with self.assertRaises(CapabilitiesUnavailable):
            self.book(self.athlete, self.command())
        self.assertEqual(self.store.appointments, {})

    def test_audit_has_no_body_or_token(self) -> None:
        with self.assertLogs("sportapp.audit", logging.INFO) as logs:
            self.book(self.athlete, self.command())

        [record] = logs.records
        fields = record.__dict__["fields"]
        self.assertEqual((fields["action"], fields["result"]), ("book", "booked"))
        self.assertEqual(fields["availability_id"], str(self.slot.availability_id))
        self.assertEqual(
            set(fields),
            {"athlete_id", "service_id", "availability_id", "appointment_id", "action", "result"},
            "solo identificadores, acción y resultado: sin cuerpo, token ni datos de salud",
        )


class CancelAppointmentTest(AppointmentCase):
    def booked(self) -> Appointment:
        return self.book(self.athlete, self.command()).appointment.appointment

    def test_cancellation_frees_the_slot_and_publishes(self) -> None:
        """CA4: `CANCELLED_BY_ATHLETE`, cupo recuperado y `sesion.cancelada.v1`."""
        appointment = self.booked()

        result = self.cancel(self.athlete, appointment.appointment_id)

        cancelled = result.appointment.appointment
        self.assertTrue(result.cancelled)
        self.assertEqual(cancelled.status, AppointmentStatus.CANCELLED_BY_ATHLETE)
        self.assertEqual(cancelled.cancelled_at, NOW)
        self.assertEqual(self.store.slots[self.slot.availability_id].booked_count, 0)
        booked_event, cancelled_event = self.store.outbox
        self.assertEqual(booked_event.event_type, BOOKED_EVENT)
        self.assertEqual(
            (cancelled_event.event_type, cancelled_event.aggregate_version),
            (CANCELLED_EVENT, 2),
        )
        self.assertIsNone(cancelled_event.causation_id)
        self.assertEqual(cancelled_event.payload["status"], "CANCELLED_BY_ATHLETE")

    def test_repeated_cancellation_changes_nothing(self) -> None:
        appointment = self.booked()
        self.cancel(self.athlete, appointment.appointment_id)
        self.clock.advance(timedelta(days=2, hours=23))

        again = self.cancel(self.athlete, appointment.appointment_id)

        self.assertFalse(again.cancelled)
        self.assertEqual(len(self.store.outbox), 2)
        self.assertEqual(self.store.slots[self.slot.availability_id].booked_count, 0)

    def test_freed_slot_can_be_booked_again(self) -> None:
        """Sin reprogramación: cancelar y reservar de nuevo."""
        appointment = self.booked()
        self.cancel(self.athlete, appointment.appointment_id)

        again = self.book(self.athlete, self.command())

        self.assertNotEqual(
            again.appointment.appointment.appointment_id, appointment.appointment_id
        )
        self.assertEqual(self.store.slots[self.slot.availability_id].booked_count, 1)

    def test_window_closed(self) -> None:
        """CA4: con menos de 24 h, `cancellation_window_closed` y nada cambia."""
        appointment = self.booked()
        self.clock.current = appointment.starts_at - timedelta(hours=2)

        with self.assertRaises(CancellationWindowClosed):
            self.cancel(self.athlete, appointment.appointment_id)
        self.assertTrue(self.store.appointments[appointment.appointment_id].is_booked)
        self.assertEqual(len(self.store.outbox), 1)

    def test_not_found_or_foreign(self) -> None:
        appointment = self.booked()
        for caller, appointment_id in (
            (Caller(uuid4(), "athlete"), appointment.appointment_id),
            (self.athlete, uuid4()),
        ):
            with self.subTest(caller=caller), self.assertRaises(AppointmentNotFound):
                self.cancel(caller, appointment_id)
        with self.assertRaises(PermissionRequired):
            self.cancel(Caller(uuid4(), "service_provider"), appointment.appointment_id)

    def test_cancellation_does_not_require_the_capability(self) -> None:
        appointment = self.booked()
        self.capabilities.granted = frozenset()

        result = self.cancel(self.athlete, appointment.appointment_id)

        self.assertTrue(result.cancelled)


class ListMyAppointmentsTest(AppointmentCase):
    def setUp(self) -> None:
        super().setUp()
        self.reader = InMemoryAppointmentReader(self.store)
        self.list = ListMyAppointments(reader=self.reader)

    def test_athlete_and_provider_see_only_their_own(self) -> None:
        """CA6 y «el profesional ve solo lo suyo»."""
        mine = self.book(self.athlete, self.command()).appointment.appointment
        other_service = self.add_service(offer("Otro entrenador"))
        other_slot = self.add_slot(slot_for(other_service))
        self.book(Caller(uuid4(), "athlete"), self.command(other_slot))
        provider = Caller(self.service.provider_id, "service_provider")

        athlete_page = self.list(self.athlete, MyAppointmentsFilters())
        provider_page = self.list(provider, MyAppointmentsFilters())

        self.assertEqual([v.appointment for v in athlete_page.items], [mine])
        self.assertEqual([v.appointment for v in provider_page.items], [mine])
        self.assertIsNone(provider_page.items[0].appointment.meeting_url)

    def test_cancelled_remain_listed_and_status_filters(self) -> None:
        appointment = self.book(self.athlete, self.command()).appointment.appointment
        self.cancel(self.athlete, appointment.appointment_id)

        everything = self.list(self.athlete, MyAppointmentsFilters())
        booked = self.list(self.athlete, MyAppointmentsFilters(status="BOOKED"))

        [item] = everything.items
        self.assertEqual(item.appointment.status, AppointmentStatus.CANCELLED_BY_ATHLETE)
        self.assertEqual(booked.items, ())

    def test_period_and_keyset(self) -> None:
        slots = [
            self.add_slot(slot_for(self.service, starts_at=NOW + timedelta(days=day)))
            for day in (2, 3, 4)
        ]
        for slot in slots:
            self.book(self.athlete, self.command(slot))

        first = self.list(self.athlete, MyAppointmentsFilters(limit=2))
        second = self.list(self.athlete, MyAppointmentsFilters(limit=2, cursor=first.next_cursor))
        period = self.list(
            self.athlete,
            MyAppointmentsFilters(
                starts_from=(NOW + timedelta(days=3)).isoformat(),
                starts_before=(NOW + timedelta(days=4)).isoformat(),
            ),
        )

        starts = [v.appointment.starts_at for v in first.items + second.items]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(len(starts), 3)
        self.assertIsNone(second.next_cursor)
        self.assertEqual([v.appointment.starts_at for v in period.items], [slots[1].starts_at])

    def test_invalid_filters(self) -> None:
        cases = [
            (MyAppointmentsFilters(starts_from=NOW.isoformat()), ("to", Issue.REQUIRED)),
            (MyAppointmentsFilters(starts_before=NOW.isoformat()), ("from", Issue.REQUIRED)),
            (
                MyAppointmentsFilters(starts_from="ayer", starts_before=NOW.isoformat()),
                ("from", Issue.UNKNOWN_VALUE),
            ),
            (
                MyAppointmentsFilters(
                    starts_from=NOW.isoformat(), starts_before="2026-10-12T15:00:00"
                ),
                ("to", Issue.UNKNOWN_VALUE),
            ),
            (
                MyAppointmentsFilters(starts_from=NOW.isoformat(), starts_before=NOW.isoformat()),
                ("to", Issue.ENDS_BEFORE_STARTS),
            ),
            (MyAppointmentsFilters(status="ALL"), ("status", Issue.UNKNOWN_VALUE)),
            (MyAppointmentsFilters(limit=0), ("limit", Issue.OUT_OF_RANGE)),
            (MyAppointmentsFilters(limit=101), ("limit", Issue.OUT_OF_RANGE)),
            (MyAppointmentsFilters(cursor="%%"), ("cursor", Issue.UNKNOWN_VALUE)),
        ]
        for filters, expected in cases:
            with self.subTest(filters=filters), self.assertRaises(InvalidServicesQuery) as raised:
                self.list(self.athlete, filters)
            self.assertEqual((raised.exception.field, raised.exception.issue), expected)

    def test_other_actors(self) -> None:
        with self.assertRaises(PermissionRequired):
            self.list(Caller(uuid4(), "organizer"), MyAppointmentsFilters())

    def test_cursor_with_invalid_identifier(self) -> None:
        with self.assertRaises(InvalidServicesQuery):
            decode_cursor("eyJzIjoiMjAyNi0xMC0xMlQxNTowMDowMCIsImkiOiJ4In0")


class ParseBookingTest(unittest.TestCase):
    def test_shapes(self) -> None:
        service_id, availability_id = uuid4(), uuid4()
        valid = {"service_id": str(service_id), "availability_id": str(availability_id)}

        self.assertEqual(parse_booking(valid), ((service_id, availability_id), []))
        self.assertEqual(parse_booking(None), (None, [Violation(None, Issue.REQUIRED)]))
        self.assertEqual(parse_booking([]), (None, [Violation(None, Issue.UNKNOWN_VALUE)]))
        self.assertEqual(
            parse_booking({"service_id": 1, "notes": "x"}),
            (
                None,
                [
                    Violation("notes", Issue.FIELD_NOT_ALLOWED),
                    Violation("service_id", Issue.UNKNOWN_VALUE),
                    Violation("availability_id", Issue.REQUIRED),
                ],
            ),
        )
        self.assertEqual(
            parse_booking({**valid, "availability_id": "x"}),
            (None, [Violation("availability_id", Issue.UNKNOWN_VALUE)]),
        )


class AppointmentsApiTest(AppointmentCase):
    def setUp(self) -> None:
        super().setUp()
        app = create_service_app(
            service_name="complementary-services",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_appointments_router(
                    verify_token=hu015.VERIFIER.verify,
                    book_appointment=self.book,
                    cancel_appointment=self.cancel,
                    list_my_appointments=ListMyAppointments(
                        reader=InMemoryAppointmentReader(self.store)
                    ),
                )
            ],
        )
        self.client = TestClient(app)
        self.headers = self.bearer(self.athlete.user_id, "athlete")

    @staticmethod
    def bearer(user_id: UUID, actor_type: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {hu015.token(user_id, actor_type)}"}

    def body(self, slot: BookableSlot | None = None) -> dict[str, str]:
        slot = slot or self.slot
        return {"service_id": str(slot.service_id), "availability_id": str(slot.availability_id)}

    def post(self, body: Any, key: str | None = None, headers: dict[str, str] | None = None) -> Any:
        sent = dict(headers or self.headers)
        if key is not None:
            sent["Idempotency-Key"] = key
        return self.client.post("/v1/me/appointments", json=body, headers=sent)

    def test_contract(self) -> None:
        """CA1, CA5 y CA6 por HTTP."""
        key = str(uuid4())

        created = self.post(self.body(), key)
        replayed = self.post(self.body(), key)
        appointment_id = created.json()["appointment_id"]
        listed = self.client.get("/v1/me/appointments", headers=self.headers)

        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.headers["Location"], f"/v1/me/appointments/{appointment_id}")
        self.assertNotIn("Idempotent-Replayed", created.headers)
        document = created.json()
        self.assertEqual(
            {k: document[k] for k in ("athlete_id", "provider_id", "service_id", "time_zone")},
            {
                "athlete_id": str(self.athlete.user_id),
                "provider_id": str(self.service.provider_id),
                "service_id": str(self.service.service_id),
                "time_zone": "America/Bogota",
            },
        )
        self.assertEqual(document["status"], "BOOKED")
        self.assertIsNone(document["meeting_url"])
        self.assertIsNone(document["cancelled_at"])
        self.assertTrue(document["starts_at"].endswith("Z"))
        self.assertEqual(
            document["service"]["fee"],
            {"amount": "120000.00", "currency": "COP", "billing_unit": "SESSION"},
        )
        self.assertEqual((replayed.status_code, replayed.json()), (201, document))
        self.assertEqual(replayed.headers["Idempotent-Replayed"], "true")
        self.assertEqual(listed.headers["Cache-Control"], "no-store")
        self.assertEqual(listed.json(), {"items": [document], "next_cursor": None})

    def test_conflicts_and_rules(self) -> None:
        """CA2 y CA3 por HTTP."""
        roomy = self.add_slot(slot_for(self.service, capacity=2))
        first = self.post(self.body(roomy), str(uuid4())).json()
        self.post(self.body(), str(uuid4()))
        draft = self.add_service(offer("Borrador", status=ServiceStatus.DRAFT))
        soon = self.add_slot(slot_for(self.service, starts_at=NOW + timedelta(minutes=10)))

        duplicate = self.post(self.body(roomy), str(uuid4()))
        full = self.post(self.body(), str(uuid4()), self.bearer(uuid4(), "athlete"))
        not_bookable = self.post(self.body(self.add_slot(slot_for(draft))), str(uuid4()))
        too_soon = self.post(self.body(soon), str(uuid4()))

        self.assertEqual(
            (duplicate.status_code, duplicate.json()["code"]), (409, "appointment_exists")
        )
        self.assertEqual(
            duplicate.json()["details"],
            [{"field": "appointment_id", "issue": first["appointment_id"]}],
        )
        self.assertEqual((full.status_code, full.json()["code"]), (409, "availability_full"))
        self.assertEqual(
            (not_bookable.status_code, not_bookable.json()["code"]), (422, "service_not_bookable")
        )
        self.assertEqual(
            (too_soon.status_code, too_soon.json()["code"]), (422, "availability_not_bookable")
        )

    def test_access_is_reported_before_validation(self) -> None:
        """CA3: `403 capability_required` aunque el cuerpo esté mal formado."""
        self.capabilities.granted = frozenset()
        provider = self.bearer(uuid4(), "service_provider")

        no_plan = self.post({"x": 1}, "no-uuid")
        other_actor = self.post({}, None, provider)
        anonymous = self.client.post("/v1/me/appointments", json={})
        self.capabilities.granted = SCHEDULE
        self.capabilities.failure = CapabilitiesUnavailable
        unavailable = self.post(self.body(), str(uuid4()))

        self.assertEqual(
            (no_plan.status_code, no_plan.json()["code"]), (403, "capability_required")
        )
        self.assertEqual(
            (other_actor.status_code, other_actor.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(
            (unavailable.status_code, unavailable.json()["code"]), (503, "upstream_unavailable")
        )

    def test_validation(self) -> None:
        invalid = self.post({"service_id": "x", "notes": "y"}, "no-uuid")
        missing_key = self.post(self.body())
        reused = str(uuid4())
        self.post(self.body(), reused)
        other = self.add_slot(slot_for(self.service, starts_at=NOW + timedelta(days=9)))
        key_reused = self.post(self.body(other), reused)

        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(
            invalid.json()["details"],
            [
                {"field": "notes", "issue": "field_not_allowed"},
                {"field": "service_id", "issue": "unknown_value"},
                {"field": "availability_id", "issue": "required"},
                {"field": "Idempotency-Key", "issue": "unknown_value"},
            ],
        )
        self.assertEqual(
            missing_key.json()["details"], [{"field": "Idempotency-Key", "issue": "required"}]
        )
        self.assertEqual(
            (key_reused.status_code, key_reused.json()["code"]), (409, "idempotency_key_reused")
        )

    def test_cancel(self) -> None:
        """CA4 por HTTP: cancelación, repetición, ventana y propiedad."""
        late_slot = self.add_slot(slot_for(self.service, starts_at=NOW + timedelta(hours=2)))
        appointment_id = self.post(self.body(), str(uuid4())).json()["appointment_id"]
        late_id = self.post(self.body(late_slot), str(uuid4())).json()["appointment_id"]
        path = f"/v1/me/appointments/{appointment_id}"

        cancelled = self.client.delete(path, headers=self.headers)
        repeated = self.client.delete(path, headers=self.headers)
        closed = self.client.delete(f"/v1/me/appointments/{late_id}", headers=self.headers)
        foreign = self.client.delete(path, headers=self.bearer(uuid4(), "athlete"))
        provider = self.client.delete(path, headers=self.bearer(uuid4(), "service_provider"))
        invalid = self.client.delete("/v1/me/appointments/x", headers=self.headers)

        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["status"], "CANCELLED_BY_ATHLETE")
        self.assertTrue(cancelled.json()["cancelled_at"].endswith("Z"))
        self.assertEqual((repeated.status_code, repeated.json()), (200, cancelled.json()))
        self.assertEqual(
            (closed.status_code, closed.json()["code"]), (422, "cancellation_window_closed")
        )
        self.assertEqual(
            (foreign.status_code, foreign.json()["code"]), (404, "appointment_not_found")
        )
        self.assertEqual(
            (provider.status_code, provider.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(invalid.status_code, 422)

    def test_list_for_provider_and_errors(self) -> None:
        self.post(self.body(), str(uuid4()))
        provider = self.bearer(self.service.provider_id, "service_provider")

        own = self.client.get("/v1/me/appointments?status=BOOKED", headers=provider)
        foreign = self.client.get(
            "/v1/me/appointments", headers=self.bearer(uuid4(), "service_provider")
        )
        organizer = self.client.get(
            "/v1/me/appointments?limit=x", headers=self.bearer(uuid4(), "organizer")
        )
        bad_limit = self.client.get("/v1/me/appointments?limit=x", headers=self.headers)
        only_from = self.client.get(
            "/v1/me/appointments?from=2026-10-12T00:00:00Z", headers=self.headers
        )

        self.assertEqual(len(own.json()["items"]), 1)
        self.assertEqual(foreign.json()["items"], [])
        self.assertEqual(
            (organizer.status_code, organizer.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(
            bad_limit.json()["details"], [{"field": "query.limit", "issue": "unknown_value"}]
        )
        self.assertEqual(only_from.json()["details"], [{"field": "query.to", "issue": "required"}])


if __name__ == "__main__":
    unittest.main()
