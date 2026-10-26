"""HU041 contra PostgreSQL real: cupo con ocho inscripciones concurrentes, unicidad,
idempotencia por operación, atomicidad, proyección y consultas del deportista."""

import threading
import unittest
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from support import EventDatabases, event_command, event_databases

from events_management.application.commands.register_for_event import RegisterForEventCommand
from events_management.application.errors import EventRegistrationExists, IdempotencyKeyReused
from events_management.application.messages import parse_sport_event_message
from events_management.application.ports import Caller
from events_management.application.projections.event_projection import ProjectionConflict
from events_management.application.queries.registrations import (
    ListMyRegistrations,
    RegistrationsFilters,
)
from events_management.domain.registrations.model import (
    EventFull,
    EventNotAvailable,
    EventRegistrationClosed,
)
from events_management.infrastructure.persistence_read.models import EventRegistrationReadRow
from events_management.infrastructure.persistence_write.models import (
    CommandIdempotencyRow,
    EventRegistrationRow,
    OutboxEventRow,
)
from events_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from events_management.workers.outbox_publisher import build_envelope


def concurrently(count: int, action: Any) -> tuple[list[Any], list[Exception]]:
    barrier = threading.Barrier(count)
    results: list[Any] = []
    errors: list[Exception] = []

    def run() -> None:
        barrier.wait()
        try:
            results.append(action())
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results, errors


class RegistrationPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: EventDatabases = self.enterContext(event_databases("hu041"))
        self.athlete = Caller(uuid4(), "athlete")

    def command(self, event_id: UUID, key: UUID | None = None) -> RegisterForEventCommand:
        return RegisterForEventCommand(
            event_id=event_id, idempotency_key=key or uuid4(), correlation_id=uuid4()
        )

    def published_messages(self) -> list[dict[str, Any]]:
        store = SqlAlchemyOutboxStore(self.db.writes)
        claimed = store.claim_batch("w-test", 100, 30)
        for event in claimed:
            store.mark_published(event.event_id, "w-test")
        return [build_envelope(event) for event in claimed]

    def test_eight_concurrent_registrations_for_the_last_seat(self) -> None:
        """CA5: exactamente una aceptada y siete `event_full`; el cupo nunca se supera."""
        event_id = self.db.publish_event(capacity=3)
        register = self.db.register()
        for _ in range(2):
            register(Caller(uuid4(), "athlete"), self.command(event_id))

        results, errors = concurrently(
            8, lambda: register(Caller(uuid4(), "athlete"), self.command(event_id))
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 7)
        self.assertTrue(all(isinstance(error, EventFull) for error in errors), errors)
        self.assertEqual(self.db.count(EventRegistrationRow), 3)
        self.assertEqual(self.db.count(OutboxEventRow), 3)

    def test_same_athlete_concurrently_with_different_keys(self) -> None:
        """CA4: una inscripción; las demás reciben la existente."""
        event_id = self.db.publish_event()
        register = self.db.register()

        results, errors = concurrently(6, lambda: register(self.athlete, self.command(event_id)))

        self.assertEqual(len(results), 1)
        self.assertTrue(all(isinstance(error, EventRegistrationExists) for error in errors), errors)
        registration_id = results[0].registration.registration.registration_id
        self.assertTrue(all(error.registration_id == registration_id for error in errors))
        self.assertEqual(self.db.count(EventRegistrationRow), 1)

    def test_same_key_concurrently_returns_the_original(self) -> None:
        """CA6: reintentos simultáneos con la misma clave → una inscripción y un evento."""
        event_id, key = self.db.publish_event(), uuid4()
        register = self.db.register()

        results, errors = concurrently(
            6, lambda: register(self.athlete, self.command(event_id, key))
        )

        self.assertEqual(errors, [])
        self.assertEqual(sum(not result.replayed for result in results), 1)
        self.assertEqual(len({r.registration.registration.registration_id for r in results}), 1)
        self.assertEqual(self.db.count(OutboxEventRow), 1)

    def test_rules_and_key_scope(self) -> None:
        """CA2, CA3 y CA6; la clave de HU021 y la de HU041 del mismo actor no se cruzan."""
        draft = self.db.publish_event(status="DRAFT")
        started = self.db.publish_event(starts_in=timedelta(minutes=-5))
        event_id = self.db.publish_event()
        key = uuid4()
        with self.assertRaises(EventNotAvailable):
            self.db.register()(self.athlete, self.command(draft))
        with self.assertRaises(EventRegistrationClosed):
            self.db.register()(self.athlete, self.command(started))
        self.db.register()(self.athlete, self.command(event_id, key))
        with self.assertRaises(IdempotencyKeyReused):
            self.db.register()(self.athlete, self.command(self.db.publish_event(), key))
        organizer = Caller(self.athlete.user_id, "organizer")  # mismo actor, otra operación
        self.db.create_event()(organizer, event_command(key))

        with self.db.writes() as session:
            operations = sorted(session.scalars(select(CommandIdempotencyRow.operation)))
        self.assertEqual(operations, ["create_event", "register_for_event"])

    def test_projection_and_athlete_queries(self) -> None:
        """CA7 y CA8: una fila aunque se repita la entrega; versión distinta → conflicto;
        «mis inscripciones» ordenada por fecha del evento y solo propias."""
        later = self.db.publish_event(starts_in=timedelta(days=40))
        sooner = self.db.publish_event(starts_in=timedelta(days=10))
        other = Caller(uuid4(), "athlete")
        for event_id in (later, sooner):
            self.db.register()(self.athlete, self.command(event_id))
        self.db.register()(other, self.command(sooner))
        project = self.db.project()
        messages = self.published_messages()
        for envelope in messages + messages:
            project(parse_sport_event_message(envelope))
        conflicting = dict(messages[0])
        conflicting["aggregate_version"] = 2
        conflicting["payload"] = {**conflicting["payload"], "version": 2}
        conflicting["event_id"] = str(uuid4())
        with self.assertRaises(ProjectionConflict):
            project(parse_sport_event_message(conflicting))

        self.assertEqual(self.db.count(EventRegistrationReadRow, read=True), 3)
        page = ListMyRegistrations(reader=self.db.registration_reader())(
            self.athlete, RegistrationsFilters()
        )
        self.assertEqual([item.registration.event_id for item in page.items], [sooner, later])
        detail = self.db.get_event()(self.athlete, sooner)
        assert detail.my_registration is not None
        self.assertEqual(detail.my_registration.athlete_id, self.athlete.user_id)
        self.assertIsNone(self.db.get_event()(Caller(uuid4(), "athlete"), later).my_registration)

    def test_keyset_does_not_lose_or_repeat(self) -> None:
        events = [self.db.publish_event(starts_in=timedelta(days=5)) for _ in range(3)] + [
            self.db.publish_event(starts_in=timedelta(days=day)) for day in (2, 9)
        ]
        for event_id in events:
            self.db.register()(self.athlete, self.command(event_id))
        project = self.db.project()
        for envelope in self.published_messages():
            project(parse_sport_event_message(envelope))
        query = ListMyRegistrations(reader=self.db.registration_reader())
        seen: list[UUID] = []
        cursor: str | None = None
        while True:
            page = query(self.athlete, RegistrationsFilters(limit=2, cursor=cursor))
            seen.extend(item.registration.event_id for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break

        self.assertEqual(sorted(seen), sorted(events))
        self.assertEqual(len(set(seen)), len(events))

    def test_draft_detail_is_not_found(self) -> None:
        draft = self.db.publish_event(status="DRAFT")

        with self.assertRaises(EventNotAvailable):
            self.db.get_event()(self.athlete, draft)


if __name__ == "__main__":
    unittest.main()
