"""HU025 contra PostgreSQL real: tarifa, versión, clave y evento en una transacción,
concurrencia, restricciones y migraciones, `applied_fee` de las inscripciones previas,
`registration_count` y proyección por estado completo con reconstrucción (CA1 a CA5)."""

import threading
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.exc import IntegrityError
from support import EventDatabases, event_command, event_databases, publish_command

from events_management.application.commands.event_fee import (
    ChangeEventFeeCommand,
    FeeChangeResult,
)
from events_management.application.commands.register_for_event import RegisterForEventCommand
from events_management.application.errors import IdempotencyKeyReused
from events_management.application.messages import parse_sport_event_message
from events_management.application.ports import Caller, PublishedEventsQuery
from events_management.application.projections.event_projection import ProjectionOutcome
from events_management.application.queries.my_events import MyEventsFilters
from events_management.domain.sport_events.fee import EventFee, FeeChange, VersionConflict
from events_management.domain.sport_events.model import EventNotFound
from events_management.domain.validation import InvalidSportEvent
from events_management.infrastructure.persistence_read.models import (
    EventReadRow,
    ProcessedEventRow,
)
from events_management.infrastructure.persistence_write.models import (
    CommandIdempotencyRow,
    EventRegistrationRow,
    EventRow,
    OutboxEventRow,
)
from events_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from events_management.workers.outbox_publisher import build_envelope
from sportapp_platform.migrations import downgrade, upgrade
from sportapp_platform.testing.postgres import temporary_database

WRITE_HISTORY = "events_management.infrastructure.persistence_write:migrations"
READ_HISTORY = "events_management.infrastructure.persistence_read:migrations"
FEE = EventFee(Decimal("85000.00"), "COP")


def concurrently(count: int, action: Callable[[], Any]) -> list[Any]:
    barrier = threading.Barrier(count)
    results: list[Any] = []

    def run() -> None:
        barrier.wait()
        try:
            results.append(action())
        except Exception as error:  # se informa para la aserción
            results.append(error)

    threads = [threading.Thread(target=run) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


def fee_command(
    event_id: UUID, version: int, fee: EventFee | None = FEE, key: UUID | None = None
) -> ChangeEventFeeCommand:
    return ChangeEventFeeCommand(
        event_id=event_id,
        change=FeeChange(fee, version),
        idempotency_key=key or uuid4(),
        correlation_id=uuid4(),
    )


class FeePersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: EventDatabases = self.enterContext(event_databases("hu025"))
        self.organizer = Caller(uuid4(), "organizer")

    def draft(self) -> UUID:
        return self.db.create_event()(self.organizer, event_command()).event.event_id

    def relay(self) -> list[dict[str, Any]]:
        store = SqlAlchemyOutboxStore(self.db.writes)
        claimed = store.claim_batch("w-test", 100, 30)
        for event in claimed:
            store.mark_published(event.event_id, "w-test")
        return [build_envelope(event) for event in claimed]

    def project_all(self, envelopes: list[dict[str, Any]]) -> list[ProjectionOutcome]:
        project = self.db.project()
        return [project(parse_sport_event_message(envelope)) for envelope in envelopes]

    def change(self, command: ChangeEventFeeCommand) -> FeeChangeResult:
        return self.db.change_fee()(self.organizer, command)

    def snapshot(self, event_id: UUID) -> dict[str, Any]:
        with self.db.reads() as session:
            row = session.get(EventReadRow, event_id)
            assert row is not None
            return {c.name: getattr(row, c.name) for c in EventReadRow.__table__.columns}

    def test_fee_version_key_and_outbox_are_committed_together(self) -> None:
        """CA1: tarifa, versión + 1, clave y `evento.tarifa_actualizada.v1` en un COMMIT."""
        event_id = self.draft()
        self.relay()
        key = uuid4()

        result = self.change(fee_command(event_id, 1, key=key))

        with self.db.writes() as session:
            row = session.get(EventRow, event_id)
            record = session.get(
                CommandIdempotencyRow, (self.organizer.user_id, "put_event_fee", key)
            )
        assert row is not None
        assert record is not None
        self.assertEqual((row.fee_amount, row.fee_currency, row.version), (FEE.amount, "COP", 2))
        self.assertEqual(row.fee_updated_at, result.event.fee_updated_at)
        self.assertEqual(record.response["registration_count"], 0)
        [envelope] = self.relay()
        self.assertEqual(envelope["event_type"], "evento.tarifa_actualizada.v1")
        self.assertEqual((envelope["aggregate_version"], envelope["causation_id"]), (2, str(key)))
        self.assertEqual(envelope["payload"]["fee"], {"amount": "85000.00", "currency": "COP"})

    def test_rejections_leave_no_partial_data(self) -> None:
        """CA2 y CA3: iniciado `422`, versión obsoleta `409`, ajeno `404`: sin cambios."""
        event_id = self.draft()
        self.relay()
        with self.assertRaises(VersionConflict):
            self.change(fee_command(event_id, 7))
        with self.assertRaises(EventNotFound):
            self.db.change_fee()(Caller(uuid4(), "organizer"), fee_command(event_id, 1))
        with self.db.writes() as session, session.begin():
            session.execute(
                text(
                    "UPDATE event SET starts_at = now() - interval '1 hour', "
                    "ends_at = now() - interval '1 hour' + make_interval(mins => duration_minutes) "
                    "WHERE event_id = :id"
                ),
                {"id": event_id},
            )
        with self.assertRaises(InvalidSportEvent) as raised:
            self.change(fee_command(event_id, 1))

        self.assertEqual(
            [(v.field, v.issue.value) for v in raised.exception.violations],
            [("starts_at", "event_already_started")],
        )
        with self.db.writes() as session:
            row = session.get(EventRow, event_id)
        assert row is not None
        self.assertEqual((row.fee_amount, row.version), (None, 1))
        self.assertEqual(self.db.count(CommandIdempotencyRow), 1, "solo la clave de la creación")
        self.assertEqual(self.relay(), [])

    def test_two_concurrent_changes_produce_one_version_and_one_event(self) -> None:
        """Ocho cambios simultáneos con la misma versión esperada: uno gana, los demás
        reciben `version_conflict`; una versión nueva y un evento."""
        event_id = self.draft()
        self.relay()

        results = concurrently(8, lambda: self.change(fee_command(event_id, 1)))

        winners = [r for r in results if isinstance(r, FeeChangeResult)]
        self.assertEqual(len(winners), 1, results)
        self.assertTrue(all(isinstance(r, VersionConflict) for r in results if r not in winners))
        self.assertEqual(winners[0].event.version, 2)
        self.assertEqual(len(self.relay()), 1)

    def test_eight_concurrent_retries_with_the_same_key(self) -> None:
        """CA5: la misma clave en paralelo → un resultado original y siete repeticiones."""
        event_id = self.draft()
        self.relay()
        key = uuid4()

        results = concurrently(8, lambda: self.change(fee_command(event_id, 1, key=key)))

        self.assertTrue(all(isinstance(r, FeeChangeResult) for r in results), results)
        self.assertEqual(sum(not r.replayed for r in results), 1)
        self.assertEqual(len({r.event.fee_updated_at for r in results}), 1)
        self.assertEqual(len(self.relay()), 1)
        with self.assertRaises(IdempotencyKeyReused):
            self.change(fee_command(event_id, 2, EventFee(Decimal("1.00"), "COP"), key))

    def test_registrations_keep_their_applied_fee_and_are_counted(self) -> None:
        """CA4 y CA5: la inscripción previa conserva su `applied_fee`; la nueva toma la tarifa
        vigente; ninguna cambia de estado ni hay cobro. `registration_count` en el comando,
        en «mis eventos» y en el detalle del propietario."""
        event_id = self.draft()
        self.db.publish()(self.organizer, publish_command(event_id))
        first = self.change(fee_command(event_id, 2, EventFee(Decimal("50000.00"), "COP")))
        earlier = self.db.register()(
            Caller(uuid4(), "athlete"), RegisterForEventCommand(event_id, uuid4())
        )

        changed = self.change(fee_command(event_id, first.event.version, FEE))
        later = self.db.register()(
            Caller(uuid4(), "athlete"), RegisterForEventCommand(event_id, uuid4())
        )
        removed = self.change(fee_command(event_id, changed.event.version, None))

        self.assertEqual((changed.registration_count, removed.registration_count), (1, 2))
        with self.db.writes() as session:
            rows = {
                row.registration_id: row
                for row in session.scalars(
                    select(EventRegistrationRow).where(EventRegistrationRow.event_id == event_id)
                )
            }
        early_row = rows[earlier.registration.registration.registration_id]
        late_row = rows[later.registration.registration.registration_id]
        self.assertEqual(early_row.applied_fee_amount, Decimal("50000.00"))
        self.assertEqual(late_row.applied_fee_amount, FEE.amount)
        self.assertEqual({row.status for row in rows.values()}, {"REGISTERED"})
        self.assertEqual({row.version for row in rows.values()}, {1})

        self.project_all(self.relay())
        [mine] = self.db.my_events()(self.organizer, MyEventsFilters()).items
        page = self.db.my_events()(self.organizer, MyEventsFilters())
        self.assertEqual((mine.fee, page.registration_count(mine)), (None, 2))
        owner = self.db.get_event()(self.organizer, event_id)
        athlete = self.db.get_event()(Caller(uuid4(), "athlete"), event_id)
        stranger = self.db.get_event()(Caller(uuid4(), "organizer"), event_id)
        self.assertEqual(owner.registration_count, 2)
        self.assertEqual((athlete.registration_count, stranger.registration_count), (None, None))

    def test_fee_constraints(self) -> None:
        event_id = self.draft()
        for sql in (
            "UPDATE event SET fee_amount = 10 WHERE event_id = :id",
            "UPDATE event SET fee_amount = 10, fee_currency = 'USD', fee_updated_at = now() "
            "WHERE event_id = :id",
            "UPDATE event SET fee_amount = -1, fee_currency = 'COP', fee_updated_at = now() "
            "WHERE event_id = :id",
            "UPDATE event SET fee_amount = 10, fee_currency = 'COP' WHERE event_id = :id",
        ):
            with self.subTest(sql=sql), self.assertRaises(IntegrityError):
                with self.db.writes() as session, session.begin():
                    session.execute(text(sql), {"id": event_id})

    def test_migrations_upgrade_existing_rows_and_downgrade(self) -> None:
        """Solo expansión: eventos y vistas existentes quedan sin tarifa; la reversión quita
        las columnas."""
        with (
            temporary_database("hu025_mig_w") as write_url,
            temporary_database("hu025_mig_r") as read_url,
        ):
            upgrade(WRITE_HISTORY, write_url, revision="0004_event_publication")
            upgrade(READ_HISTORY, read_url, revision="0006_registration_event_order")
            engine = create_engine(write_url)
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO event VALUES (:id, :org, 'Carrera 10K', "
                            "'Descripción suficientemente larga.', 'Running', 'Parque central', "
                            "'Calle 1 # 2-3', 'Bogotá', 'CO', :start, :end, 'America/Bogota', "
                            "180, 100, 'DRAFT', 1, :at, :at, NULL)"
                        ),
                        {
                            "id": uuid4(),
                            "org": uuid4(),
                            "start": datetime(2099, 1, 1, tzinfo=UTC),
                            "end": datetime(2099, 1, 1, 3, tzinfo=UTC),
                            "at": datetime(2026, 10, 1, tzinfo=UTC),
                        },
                    )
                upgrade(WRITE_HISTORY, write_url)
                upgrade(READ_HISTORY, read_url)
                with engine.connect() as connection:
                    fees = connection.execute(
                        text("SELECT fee_amount, fee_currency, fee_updated_at FROM event")
                    ).all()
                downgrade(WRITE_HISTORY, write_url, revision="0004_event_publication")
                with engine.connect() as connection:
                    columns = connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'event' AND column_name LIKE 'fee%'"
                        )
                    ).all()
            finally:
                engine.dispose()
        self.assertEqual(fees, [(None, None, None)])
        self.assertEqual(columns, [])

    def test_projection_by_full_state_out_of_order_and_rebuild(self) -> None:
        """CA1: la tarifa llega a `event_read` por versión mayor aunque el mensaje de la
        publicación (que también la transporta) llegue antes; duplicados y versiones menores
        no la cambian; la reconstrucción da el mismo resultado y el detalle y el calendario
        públicos la exponen."""
        event_id = self.draft()
        created = self.relay()
        self.change(fee_command(event_id, 1))
        priced = self.relay()
        self.db.publish()(self.organizer, publish_command(event_id))
        published = self.relay()
        envelopes = created + priced + published

        outcomes = self.project_all([published[0], priced[0], created[0], published[0]])

        self.assertEqual(
            outcomes,
            [
                ProjectionOutcome.PROJECTED,
                ProjectionOutcome.STALE,
                ProjectionOutcome.STALE,
                ProjectionOutcome.DUPLICATE,
            ],
        )
        snapshot = self.snapshot(event_id)
        self.assertEqual((snapshot["status"], snapshot["version"]), ("PUBLISHED", 3))
        self.assertEqual((snapshot["fee_amount"], snapshot["fee_currency"]), (FEE.amount, "COP"))
        self.assertIsNotNone(snapshot["fee_updated_at"])

        with self.db.reads() as session, session.begin():
            session.execute(delete(EventReadRow))
            session.execute(delete(ProcessedEventRow))
        self.assertEqual(self.project_all(envelopes), [ProjectionOutcome.PROJECTED] * 3)
        rebuilt = self.snapshot(event_id)
        self.assertEqual(
            {k: v for k, v in rebuilt.items() if k != "projected_at"},
            {k: v for k, v in snapshot.items() if k != "projected_at"},
        )

        detail = self.db.detail_reader().visible_detail(event_id, athlete_id=uuid4(), owner_id=None)
        assert detail is not None
        self.assertEqual(detail.event.fee, FEE)
        now = datetime.now(UTC)
        [item] = self.db.published_reader().published(
            PublishedEventsQuery(
                starts_before_or_at=now + timedelta(days=365 * 80),
                ends_at_or_after=now,
                after=None,
                limit=10,
            )
        )
        self.assertEqual(item.fee, FEE)

    def test_removal_is_projected(self) -> None:
        event_id = self.draft()
        self.change(fee_command(event_id, 1))
        self.change(fee_command(event_id, 2, None))
        envelopes = self.relay()

        self.project_all(envelopes)

        snapshot = self.snapshot(event_id)
        self.assertEqual((snapshot["fee_amount"], snapshot["version"]), (None, 3))
        self.assertIsNotNone(snapshot["fee_updated_at"])
        with self.db.writes() as session:
            count = session.scalar(
                select(OutboxEventRow.event_id)
                .where(OutboxEventRow.event_type == "evento.tarifa_actualizada.v1")
                .limit(1)
            )
        self.assertIsNotNone(count)


if __name__ == "__main__":
    unittest.main()
