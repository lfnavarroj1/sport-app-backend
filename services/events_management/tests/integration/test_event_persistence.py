"""HU021 contra PostgreSQL real: atomicidad, idempotencia concurrente, outbox, proyección
y visibilidad del borrador (CA1, CA4, CA5, CA6)."""

import threading
import unittest
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import DataError
from support import EventDatabases, OrganizerOnly, event_command, event_databases

from events_management.application.commands.create_event import CreatedEvent, CreateEvent
from events_management.application.errors import IdempotencyKeyReused
from events_management.application.messages import parse_sport_event_message
from events_management.application.ports import Caller, OutboxMessage
from events_management.application.projections.event_projection import ProjectionOutcome
from events_management.infrastructure.clock import SystemClock
from events_management.infrastructure.persistence_read.models import (
    EventReadRow,
    ProcessedEventRow,
)
from events_management.infrastructure.persistence_write.models import (
    CommandIdempotencyRow,
    EventRow,
    OutboxEventRow,
)
from events_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from events_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyEventCreationUnitOfWork,
)
from events_management.workers.outbox_publisher import build_envelope


class BrokenOutboxUnitOfWork(SqlAlchemyEventCreationUnitOfWork):
    """Falla al guardar `evento.creado.v1`: nada de la transacción debe confirmarse."""

    def add_outbox(self, message: OutboxMessage) -> None:
        super().add_outbox(
            OutboxMessage(**{**message.__dict__, "aggregate_type": "x" * 65})  # > String(64)
        )


class EventPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: EventDatabases = self.enterContext(event_databases("hu021"))
        self.organizer = Caller(uuid4(), "organizer")

    def published_messages(self) -> list[dict[str, object]]:
        store = SqlAlchemyOutboxStore(self.db.writes)
        claimed = store.claim_batch("w-test", 100, 30)
        for event in claimed:
            store.mark_published(event.event_id, "w-test")
        return [build_envelope(event) for event in claimed]

    def test_event_outbox_and_key_are_committed_together(self) -> None:
        """CA1 y CA5: agregado, `evento.creado.v1` y clave en la misma transacción."""
        result = self.db.create_event()(self.organizer, event_command())

        with self.db.writes() as session:
            row = session.get(EventRow, result.event.event_id)
            outbox = session.scalars(select(OutboxEventRow)).one()
            key = session.scalars(select(CommandIdempotencyRow)).one()
        assert row is not None
        self.assertEqual((row.status, row.version), ("DRAFT", 1))
        self.assertEqual(row.organizer_id, self.organizer.user_id)
        self.assertEqual(row.starts_at, datetime(2099, 11, 15, 12, 0, tzinfo=UTC))
        self.assertEqual(row.ends_at, datetime(2099, 11, 15, 15, 0, tzinfo=UTC))
        self.assertEqual(row.time_zone, "America/Bogota")
        self.assertEqual(
            (outbox.event_type, outbox.aggregate_id), ("evento.creado.v1", row.event_id)
        )
        self.assertIsNone(outbox.published_at)
        self.assertEqual((key.actor_id, key.resource_id), (self.organizer.user_id, row.event_id))

    def test_outbox_failure_rolls_back_the_event(self) -> None:
        """CA5: sin outbox no hay agregado ni clave (todo o nada)."""
        create = CreateEvent(
            unit_of_work=lambda: BrokenOutboxUnitOfWork(self.db.writes),
            permissions=OrganizerOnly(),
            clock=SystemClock(),
        )

        with self.assertRaises(DataError):
            create(self.organizer, event_command())

        self.assertEqual(
            [self.db.count(model) for model in (EventRow, OutboxEventRow, CommandIdempotencyRow)],
            [0, 0, 0],
        )

    def test_concurrent_requests_with_the_same_key_create_one_event(self) -> None:
        """CA4: carrera real en PostgreSQL; un evento y un `evento.creado.v1`."""
        key = uuid4()
        barrier = threading.Barrier(6)
        results: list[CreatedEvent] = []
        errors: list[Exception] = []

        def attempt() -> None:
            create = self.db.create_event()
            barrier.wait()
            try:
                results.append(create(self.organizer, event_command(key)))
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=attempt) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(errors, [])
        self.assertEqual(len({result.event.event_id for result in results}), 1)
        self.assertEqual(sum(not result.replayed for result in results), 1)
        self.assertEqual((self.db.count(EventRow), self.db.count(OutboxEventRow)), (1, 1))

    def test_key_scope_and_reuse(self) -> None:
        """CA4: otro payload → reutilizada; la misma clave de otro organizador es independiente."""
        key = uuid4()
        self.db.create_event()(self.organizer, event_command(key))

        with self.assertRaises(IdempotencyKeyReused):
            self.db.create_event()(self.organizer, event_command(key, capacity=10))
        other = self.db.create_event()(Caller(uuid4(), "organizer"), event_command(key))

        self.assertFalse(other.replayed)
        self.assertEqual((self.db.count(EventRow), self.db.count(OutboxEventRow)), (2, 2))

    def test_outbox_publishes_each_event_once(self) -> None:
        self.db.create_event()(self.organizer, event_command())

        first, second = self.published_messages(), self.published_messages()

        self.assertEqual((len(first), second), (1, []))

    def test_projection_is_idempotent_and_drafts_stay_private(self) -> None:
        """CA5 y CA6: una sola fila en `event_read` aunque se repita la entrega; el borrador
        solo lo ve su organizador y no aparece en el catálogo público."""
        result = self.db.create_event()(self.organizer, event_command())
        [published] = self.published_messages()
        message = parse_sport_event_message(published)
        republished = parse_sport_event_message({**published, "event_id": str(uuid4())})
        project = self.db.project()

        outcomes = [project(message), project(message), project(republished)]

        self.assertEqual(
            outcomes,
            [ProjectionOutcome.PROJECTED, ProjectionOutcome.DUPLICATE, ProjectionOutcome.STALE],
        )
        self.assertEqual(self.db.count(EventReadRow, read=True), 1)
        self.assertEqual(self.db.count(ProcessedEventRow, read=True), 2)
        reader = self.db.reader()
        self.assertEqual(
            reader.owned_by(self.organizer.user_id, result.event.event_id), result.event
        )
        self.assertIsNone(reader.owned_by(uuid4(), result.event.event_id))
        self.assertEqual(reader.public_events(), [])

    def test_concurrent_deliveries_project_once(self) -> None:
        self.db.create_event()(self.organizer, event_command())
        [published] = self.published_messages()
        message = parse_sport_event_message(published)
        barrier = threading.Barrier(4)
        outcomes: list[ProjectionOutcome] = []

        def deliver() -> None:
            project = self.db.project()
            barrier.wait()
            outcomes.append(project(message))

        threads = [threading.Thread(target=deliver) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(outcomes.count(ProjectionOutcome.PROJECTED), 1)
        self.assertEqual(len(outcomes), 4)
        self.assertEqual(self.db.count(EventReadRow, read=True), 1)
        self.assertEqual(self.db.count(ProcessedEventRow, read=True), 1)


if __name__ == "__main__":
    unittest.main()
