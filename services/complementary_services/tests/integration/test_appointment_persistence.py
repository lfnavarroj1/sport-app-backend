"""HU040 con PostgreSQL real: bloqueo del intervalo, capacidad 1 con 20 reservas
concurrentes, cita duplicada, idempotencia, cancelación, outbox, consulta por actor y
migración `0006_appointments` sobre intervalos existentes (ADR-046)."""

import unittest
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, func, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from test_publication_persistence import Providers, concurrently
from test_service_persistence import future_body

from complementary_services.api.payload import parse_offer
from complementary_services.application.commands.appointments import (
    BOOKED_EVENT,
    CANCELLED_EVENT,
    VIRTUAL_SESSIONS_CAPABILITY,
    BookAppointment,
    BookAppointmentCommand,
    BookedAppointment,
    CancelAppointment,
    VirtualSessionAccess,
)
from complementary_services.application.commands.create_service import (
    CreateService,
    CreateServiceCommand,
)
from complementary_services.application.commands.publish_service import (
    PublishService,
    PublishServiceCommand,
)
from complementary_services.application.errors import (
    AppointmentExists,
    AvailabilityFull,
    CancellationWindowClosed,
    CapabilityRequired,
    IdempotencyKeyReused,
    ServiceNotBookable,
)
from complementary_services.application.ports import AvailabilityQuery, Caller
from complementary_services.application.queries.my_appointments import (
    ListMyAppointments,
    MyAppointmentsFilters,
)
from complementary_services.infrastructure.clock import SystemClock
from complementary_services.infrastructure.persistence.appointments_reader import (
    SqlAlchemyAppointmentReader,
)
from complementary_services.infrastructure.persistence.catalog_reader import (
    SqlAlchemyServiceCatalogReader,
)
from complementary_services.infrastructure.persistence.models import (
    AppointmentRow,
    IdempotencyKeyRow,
    OutboxEventRow,
    ServiceAvailabilityRow,
)
from complementary_services.infrastructure.persistence.unit_of_work import (
    SqlAlchemyAppointmentUnitOfWork,
    SqlAlchemyCreationUnitOfWork,
    SqlAlchemyPublicationUnitOfWork,
)
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.postgres import temporary_database

HISTORY = "complementary_services.infrastructure.persistence:migrations"
VIRTUAL_COACHING = {"category": "COACHING", "modality": "VIRTUAL"}


class Capabilities:
    def __init__(self) -> None:
        self.granted = frozenset({VIRTUAL_SESSIONS_CAPABILITY})

    def has_capability(self, user_id: UUID, capability: str) -> bool:
        return capability in self.granted


class ShiftedClock:
    """La hora real desplazada: permite cancelar «a 2 h» de una cita real."""

    def __init__(self) -> None:
        self.offset = timedelta()

    def now(self) -> Any:
        return SystemClock().now() + self.offset


class AppointmentPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        url = self.enterContext(temporary_database("hu040"))
        upgrade(HISTORY, url)
        engine = create_engine(url, pool_size=25, max_overflow=5)
        self.addCleanup(engine.dispose)
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        self.capabilities = Capabilities()
        self.clock = ShiftedClock()
        self.book = BookAppointment(
            unit_of_work=lambda: SqlAlchemyAppointmentUnitOfWork(self.sessions),
            access=VirtualSessionAccess(self.capabilities),
            clock=self.clock,
        )
        self.cancel = CancelAppointment(
            unit_of_work=lambda: SqlAlchemyAppointmentUnitOfWork(self.sessions), clock=self.clock
        )
        self.list = ListMyAppointments(reader=SqlAlchemyAppointmentReader(self.sessions))
        self.provider = Caller(uuid4(), "service_provider")
        self.athlete = Caller(uuid4(), "athlete")
        self.service_id = self.service(**VIRTUAL_COACHING)
        # `future_body`: el primer intervalo tiene capacidad 3 y el segundo capacidad 1.
        self.roomy, self.single = self.intervals(self.service_id)

    def service(self, *, publish: bool = True, provider: Caller | None = None, **body: Any) -> UUID:
        owner = provider or self.provider
        created = CreateService(
            unit_of_work=lambda: SqlAlchemyCreationUnitOfWork(self.sessions),
            permissions=Providers(),
            clock=SystemClock(),
        )(
            owner,
            CreateServiceCommand(offer=parse_offer(future_body(**body)), idempotency_key=uuid4()),
        )
        service_id = created.service.offer.service_id
        if publish:
            PublishService(
                unit_of_work=lambda: SqlAlchemyPublicationUnitOfWork(self.sessions),
                permissions=Providers(),
                clock=SystemClock(),
            )(owner, PublishServiceCommand(service_id=service_id, idempotency_key=uuid4()))
        return service_id

    def intervals(self, service_id: UUID) -> list[UUID]:
        with self.sessions() as session:
            return list(
                session.scalars(
                    select(ServiceAvailabilityRow.availability_id)
                    .where(ServiceAvailabilityRow.service_id == service_id)
                    .order_by(ServiceAvailabilityRow.position)
                )
            )

    def command(self, availability_id: UUID, key: UUID | None = None) -> BookAppointmentCommand:
        return BookAppointmentCommand(
            service_id=self.service_id,
            availability_id=availability_id,
            idempotency_key=key or uuid4(),
            correlation_id=uuid4(),
        )

    def booked_count(self, availability_id: UUID) -> int:
        with self.sessions() as session:
            return int(
                session.scalar(
                    select(ServiceAvailabilityRow.booked_count).where(
                        ServiceAvailabilityRow.availability_id == availability_id
                    )
                )
                or 0
            )

    def outbox(self) -> list[OutboxEventRow]:
        with self.sessions() as session:
            return list(
                session.scalars(select(OutboxEventRow).order_by(OutboxEventRow.occurred_at))
            )

    def appointments(self) -> list[AppointmentRow]:
        with self.sessions() as session:
            return list(session.scalars(select(AppointmentRow)))

    def test_booking_commits_appointment_counter_key_and_event_together(self) -> None:
        """CA1: cita `BOOKED`, contador, clave y `sesion.programada.v1` en un COMMIT."""
        key = uuid4()

        result = self.book(self.athlete, self.command(self.single, key))

        [row] = self.appointments()
        appointment = result.appointment.appointment
        self.assertEqual(
            (row.appointment_id, row.athlete_id, row.provider_id, row.status, row.version),
            (
                appointment.appointment_id,
                self.athlete.user_id,
                self.provider.user_id,
                "BOOKED",
                1,
            ),
        )
        self.assertIsNone(row.meeting_url)
        self.assertEqual(self.booked_count(self.single), 1)
        events = [event for event in self.outbox() if event.event_type == BOOKED_EVENT]
        [event] = events
        self.assertEqual(
            (event.aggregate_type, event.aggregate_id, event.causation_id),
            ("appointment", appointment.appointment_id, str(key)),
        )
        self.assertEqual(event.payload["availability_id"], str(self.single))
        with self.sessions() as session:
            self.assertIsNotNone(session.get(IdempotencyKeyRow, (self.athlete.user_id, key)))

    def test_twenty_concurrent_bookings_on_capacity_one(self) -> None:
        """CA2 / HA018: con capacidad 1 y 20 deportistas, una confirmada y 19
        `availability_full`."""
        pending = iter([Caller(uuid4(), "athlete") for _ in range(20)])

        def attempt() -> BookedAppointment:
            return self.book(next(pending), self.command(self.single))

        results, errors = concurrently(20, attempt)

        self.assertEqual(len(results), 1, errors)
        self.assertEqual(len(errors), 19)
        self.assertTrue(all(isinstance(error, AvailabilityFull) for error in errors), errors)
        self.assertEqual(self.booked_count(self.single), 1)
        self.assertEqual(len(self.appointments()), 1)
        self.assertEqual(len([e for e in self.outbox() if e.event_type == BOOKED_EVENT]), 1)

    def test_concurrent_duplicates_of_same_athlete(self) -> None:
        """CA2: el mismo deportista con claves distintas sobre un intervalo con cupo: una
        cita y las demás `appointment_exists`."""
        results, errors = concurrently(8, lambda: self.book(self.athlete, self.command(self.roomy)))

        self.assertEqual(len(results), 1, errors)
        self.assertTrue(all(isinstance(error, AppointmentExists) for error in errors), errors)
        self.assertEqual(self.booked_count(self.roomy), 1)

    def test_concurrent_retries_with_same_key(self) -> None:
        """CA5: la misma clave en paralelo devuelve la respuesta original."""
        command = self.command(self.roomy)

        results, errors = concurrently(6, lambda: self.book(self.athlete, command))

        self.assertEqual(errors, [])
        self.assertEqual(len({r.appointment.appointment.appointment_id for r in results}), 1)
        self.assertEqual(sum(not r.replayed for r in results), 1)
        self.assertEqual(self.booked_count(self.roomy), 1)

    def test_key_reused_and_partial_index(self) -> None:
        key = uuid4()
        self.book(self.athlete, self.command(self.roomy, key))

        with self.assertRaises(IdempotencyKeyReused):
            self.book(self.athlete, self.command(self.single, key))
        [row] = self.appointments()
        with self.sessions() as session, self.assertRaises(IntegrityError):
            session.execute(
                insert(AppointmentRow).values(
                    {
                        **{c.name: getattr(row, c.name) for c in AppointmentRow.__table__.columns},
                        "appointment_id": uuid4(),
                    }
                )
            )
            session.commit()

    def test_counter_never_exceeds_capacity(self) -> None:
        with self.sessions() as session, self.assertRaises(IntegrityError):
            session.execute(
                text(
                    "UPDATE service_availability SET booked_count = capacity + 1 "
                    "WHERE availability_id = :id"
                ),
                {"id": self.single},
            )
            session.commit()

    def test_cancellation_frees_the_slot_and_publishes(self) -> None:
        """CA4: cancelar a tiempo libera el cupo y otro deportista lo toma."""
        result = self.book(self.athlete, self.command(self.single))
        appointment_id = result.appointment.appointment.appointment_id

        cancelled = self.cancel(self.athlete, appointment_id)
        again = self.cancel(self.athlete, appointment_id)
        other = self.book(Caller(uuid4(), "athlete"), self.command(self.single))

        self.assertTrue(cancelled.cancelled)
        self.assertFalse(again.cancelled)
        self.assertEqual(self.booked_count(self.single), 1)
        self.assertEqual(other.appointment.appointment.status.value, "BOOKED")
        types = [
            event.event_type for event in self.outbox() if event.aggregate_type == "appointment"
        ]
        self.assertEqual(types, [BOOKED_EVENT, CANCELLED_EVENT, BOOKED_EVENT])
        [cancelled_event] = [e for e in self.outbox() if e.event_type == CANCELLED_EVENT]
        self.assertEqual(cancelled_event.aggregate_version, 2)
        self.assertEqual(cancelled_event.payload["status"], "CANCELLED_BY_ATHLETE")

    def test_cancellation_two_hours_before_is_rejected(self) -> None:
        """CA4: a 2 h del inicio, `cancellation_window_closed` y el cupo sigue ocupado."""
        result = self.book(self.athlete, self.command(self.single))
        appointment = result.appointment.appointment
        self.clock.offset = appointment.starts_at - timedelta(hours=2) - SystemClock().now()

        with self.assertRaises(CancellationWindowClosed):
            self.cancel(self.athlete, appointment.appointment_id)
        self.assertEqual(self.booked_count(self.single), 1)
        [row] = self.appointments()
        self.assertEqual(row.status, "BOOKED")

    def test_ineligible_service_and_missing_capability(self) -> None:
        """CA3: servicio presencial `service_not_bookable`; sin capacidad, nada se escribe."""
        in_person = self.service(category="COACHING", modality="IN_PERSON")
        [slot, _] = self.intervals(in_person)
        with self.assertRaises(ServiceNotBookable):
            self.book(
                self.athlete,
                BookAppointmentCommand(
                    service_id=in_person, availability_id=slot, idempotency_key=uuid4()
                ),
            )
        self.capabilities.granted = frozenset()
        with self.assertRaises(CapabilityRequired):
            self.book(self.athlete, self.command(self.single))
        self.assertEqual(self.appointments(), [])

    def test_provider_sees_only_appointments_of_own_services(self) -> None:
        """CA6: deportista y profesional consultan la cita, también cancelada; otro
        profesional no la ve."""
        mine = self.book(self.athlete, self.command(self.single)).appointment.appointment
        other_provider = Caller(uuid4(), "service_provider")
        other_service = self.service(provider=other_provider, **VIRTUAL_COACHING)
        [other_slot, _] = self.intervals(other_service)
        self.book(
            Caller(uuid4(), "athlete"),
            BookAppointmentCommand(
                service_id=other_service, availability_id=other_slot, idempotency_key=uuid4()
            ),
        )
        self.cancel(self.athlete, mine.appointment_id)

        athlete_page = self.list(self.athlete, MyAppointmentsFilters())
        provider_page = self.list(self.provider, MyAppointmentsFilters())
        booked_only = self.list(self.provider, MyAppointmentsFilters(status="BOOKED"))

        for page in (athlete_page, provider_page):
            [view] = page.items
            self.assertEqual(view.appointment.appointment_id, mine.appointment_id)
            self.assertEqual(view.appointment.status.value, "CANCELLED_BY_ATHLETE")
            self.assertEqual(view.service.name, "Valoración deportiva inicial")
        self.assertEqual(booked_only.items, ())

    def test_keyset_and_period(self) -> None:
        second_service = self.service(**VIRTUAL_COACHING)
        slots = [(self.service_id, self.roomy), (self.service_id, self.single)] + [
            (second_service, item) for item in self.intervals(second_service)
        ]
        for service_id, slot in slots:
            self.book(
                self.athlete,
                BookAppointmentCommand(
                    service_id=service_id, availability_id=slot, idempotency_key=uuid4()
                ),
            )

        first = self.list(self.athlete, MyAppointmentsFilters(limit=3))
        second = self.list(self.athlete, MyAppointmentsFilters(limit=3, cursor=first.next_cursor))
        start = first.items[0].appointment.starts_at
        period = self.list(
            self.athlete,
            MyAppointmentsFilters(
                starts_from=start.isoformat(),
                starts_before=(start + timedelta(minutes=1)).isoformat(),
            ),
        )

        keys = [
            (v.appointment.starts_at, v.appointment.appointment_id)
            for v in first.items + second.items
        ]
        self.assertEqual(len(keys), 4)
        self.assertEqual(keys, sorted(keys))
        self.assertIsNone(second.next_cursor)
        self.assertEqual(len(period.items), 2)

    def test_catalog_exposes_the_interval_identifier(self) -> None:
        """El catálogo de HU020 devuelve el `availability_id` con el que se reserva."""
        now = SystemClock().now()
        [offer] = SqlAlchemyServiceCatalogReader(self.sessions).published_availability(
            AvailabilityQuery(
                starts_from=now,
                starts_before=now + timedelta(days=31),
                now=now,
                categories=None,
                modalities=None,
                only_available=True,
                after=None,
                limit=10,
            )
        )

        self.assertEqual(
            [interval.availability_id for interval in offer.availability],
            [self.roomy, self.single],
        )


class AppointmentMigrationTest(unittest.TestCase):
    def test_existing_intervals_get_identifiers_and_empty_counters(self) -> None:
        """`0006_appointments` identifica los intervalos ya guardados sin relleno aparte."""
        with temporary_database("hu040_migration") as url:
            upgrade(HISTORY, url, "0005_service_association")
            engine = create_engine(url)
            self.addCleanup(engine.dispose)
            service_id = uuid4()
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO service (service_id, provider_id, name, description, "
                        "category, modality, price_amount, currency, billing_unit, status, "
                        "version, created_at, updated_at) VALUES (:s, :p, 'Servicio previo', "
                        "'Servicio sintético guardado antes de HU040.', 'COACHING', 'VIRTUAL', "
                        "1000, 'COP', 'SESSION', 'DRAFT', 1, now(), now())"
                    ),
                    {"s": service_id, "p": uuid4()},
                )
                for position in range(3):
                    connection.execute(
                        text(
                            "INSERT INTO service_availability (service_id, position, starts_at, "
                            "ends_at, time_zone, capacity) VALUES (:s, :n, "
                            "now() + make_interval(days => :n + 1), "
                            "now() + make_interval(days => :n + 1, hours => 1), "
                            "'America/Bogota', 2)"
                        ),
                        {"s": service_id, "n": position},
                    )

            upgrade(HISTORY, url)

            with engine.connect() as connection:
                rows = connection.execute(
                    text("SELECT availability_id, booked_count FROM service_availability")
                ).all()
                appointments = connection.execute(
                    select(func.count()).select_from(AppointmentRow)
                ).scalar_one()
        self.assertEqual(len({row.availability_id for row in rows}), 3)
        self.assertTrue(all(row.availability_id is not None for row in rows))
        self.assertEqual({row.booked_count for row in rows}, {0})
        self.assertEqual(appointments, 0)


if __name__ == "__main__":
    unittest.main()
