"""HU022 (ADR-039) contra PostgreSQL real (requiere `TEST_DATABASE_URL`): inscripciones reales
de HU041 proyectadas y listadas por el organizador, keyset con 57 inscripciones sin pérdidas ni
repeticiones, evento ajeno o inexistente → no encontrado y migración de lectura `0006`."""

import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, insert, inspect, pool, select
from support import EventDatabases, event_databases

from events_management.application.commands.register_for_event import RegisterForEventCommand
from events_management.application.messages import parse_sport_event_message
from events_management.application.ports import Caller
from events_management.application.queries.attendees import (
    AttendeesFilters,
    ListEventAttendees,
)
from events_management.domain.registrations.model import EventNotAvailable
from events_management.infrastructure.persistence_read.events import SqlAlchemyAttendeeReader
from events_management.infrastructure.persistence_read.models import (
    EventReadRow,
    EventRegistrationReadRow,
)
from events_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from events_management.workers.outbox_publisher import build_envelope
from sportapp_platform.migrations import current_revision, downgrade, upgrade
from sportapp_platform.testing.postgres import temporary_database

READ_HISTORY = "events_management.infrastructure.persistence_read:migrations"
INDEX = "ix_event_registration_read_event_order"


class AttendeesPermission:
    def allows(self, actor_type: str, permission: str) -> bool:
        return actor_type == "organizer" and permission == "events.attendees.view"


class AttendeesPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: EventDatabases = self.enterContext(event_databases("hu022"))
        self.list = ListEventAttendees(
            reader=SqlAlchemyAttendeeReader(self.db.reads), permissions=AttendeesPermission()
        )

    def organizer_of(self, event_id: UUID) -> Caller:
        with self.db.reads() as session:
            organizer_id = session.scalar(
                select(EventReadRow.organizer_id).where(EventReadRow.event_id == event_id)
            )
        assert organizer_id is not None
        return Caller(organizer_id, "organizer")

    def project_outbox(self) -> None:
        store, project = SqlAlchemyOutboxStore(self.db.writes), self.db.project()
        for event in store.claim_batch("w-test", 100, 30):
            project(parse_sport_event_message(build_envelope(event)))
            store.mark_published(event.event_id, "w-test")

    def test_real_registrations_reach_the_organizer_list(self) -> None:
        """CA1 y CA5: tres inscripciones de HU041, ya proyectadas, aparecen con conteos."""
        event_id = self.db.publish_event(capacity=10)
        organizer = self.organizer_of(event_id)
        empty = self.list(organizer, event_id, AttendeesFilters())
        athletes = [Caller(uuid4(), "athlete") for _ in range(3)]
        for athlete in athletes:
            self.db.register()(
                athlete,
                RegisterForEventCommand(
                    event_id=event_id, idempotency_key=uuid4(), correlation_id=uuid4()
                ),
            )

        self.project_outbox()
        result = self.list(organizer, event_id, AttendeesFilters(status="ALL"))

        self.assertEqual((empty.page.items, empty.total), ((), 0))
        self.assertEqual(result.total, 3)
        self.assertEqual(result.page.by_status, {"REGISTERED": 3})
        self.assertEqual(
            {item.athlete_id for item in result.page.items},
            {athlete.user_id for athlete in athletes},
        )

    def test_keyset_over_57_registrations_does_not_lose_or_repeat(self) -> None:
        """CA4: orden `registered_at`, `registration_id` estable, con empates de fecha."""
        event_id = self.db.publish_event(capacity=100)
        other_event = self.db.publish_event(capacity=100)
        organizer = self.organizer_of(event_id)
        base = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
        rows = [_row(event_id, base + timedelta(minutes=index // 3)) for index in range(57)]
        rows += [_row(other_event, base) for _ in range(5)]
        with self.db.reads() as session, session.begin():
            session.execute(insert(EventRegistrationReadRow), rows)

        seen: list[UUID] = []
        cursor = None
        pages = 0
        while True:
            result = self.list(organizer, event_id, AttendeesFilters(limit=10, cursor=cursor))
            pages += 1
            seen.extend(item.registration_id for item in result.page.items)
            self.assertEqual((result.total, result.page.by_status), (57, {"REGISTERED": 57}))
            cursor = result.next_cursor
            if cursor is None:
                break

        expected = [
            row["registration_id"]
            for row in sorted(
                (row for row in rows if row["event_id"] == event_id),
                key=lambda row: (row["registered_at"], row["registration_id"]),
            )
        ]
        self.assertEqual(seen, expected)
        self.assertEqual(pages, 6)

    def test_foreign_or_missing_event_is_not_found(self) -> None:
        """CA2: otro organizador o un evento inexistente no revelan participantes."""
        event_id = self.db.publish_event()
        with self.db.reads() as session, session.begin():
            session.execute(insert(EventRegistrationReadRow), [_row(event_id, datetime.now(UTC))])

        for caller, target in (
            (Caller(uuid4(), "organizer"), event_id),
            (self.organizer_of(event_id), uuid4()),
        ):
            with self.subTest(target=str(target)), self.assertRaises(EventNotAvailable):
                self.list(caller, target, AttendeesFilters())


class AttendeesMigrationTest(unittest.TestCase):
    def test_index_is_created_and_reverted(self) -> None:
        with temporary_database("events_read_0006") as url:
            upgrade(READ_HISTORY, url)
            self.assertEqual(current_revision(url), "0006_registration_event_order")
            self.assertIn(INDEX, _indexes(url))

            downgrade(READ_HISTORY, url, "0005_event_read_publication")

            self.assertNotIn(INDEX, _indexes(url))
            upgrade(READ_HISTORY, url)
            self.assertEqual(current_revision(url), "0006_registration_event_order")


def _row(event_id: UUID, registered_at: datetime) -> dict[str, Any]:
    return {
        "registration_id": uuid4(),
        "event_id": event_id,
        "athlete_id": uuid4(),
        "status": "REGISTERED",
        "registered_at": registered_at,
        "applied_fee_amount": None,
        "applied_fee_currency": None,
        "version": 1,
        "projected_at": registered_at,
    }


def _indexes(url: str) -> set[str]:
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        return {
            str(index["name"]) for index in inspect(engine).get_indexes("event_registration_read")
        }
    finally:
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
