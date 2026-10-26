"""HU031 (ADR-042 §12) contra PostgreSQL real (requiere `TEST_DATABASE_URL`): migración
`0015_session_history_source` ida y vuelta, y el consumidor de `actividad.importada.v1`
escribiendo en la base de lectura (inserción, duplicado por `event_id`, versión mayor que
reemplaza, menor o igual que se ignora, entregas concurrentes, sesión de HU032 intacta) y
el historial de HU011 con `source`."""

import json
import threading
import unittest
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, insert, inspect, pool, select, text
from support import READ_HISTORY, TrainingDatabases, training_databases

from sportapp_platform.migrations import current_revision, downgrade, upgrade
from sportapp_platform.testing.postgres import temporary_database
from training_management.application.ports import Caller
from training_management.application.projections.external_activity_projection import (
    ProjectExternalActivity,
)
from training_management.application.queries.training_history import HistoryFilters
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.persistence_read.external_activities import (
    SqlAlchemyExternalActivityUnitOfWork,
)
from training_management.infrastructure.persistence_read.models import (
    ProcessedEventRow,
    TrainingSessionHistoryRow,
)
from training_management.workers.external_activity_consumer import (
    ExternalActivityMessageHandler,
)

HEAD = "0015_session_history_source"
PREVIOUS = "0013_health_report_view"
NEW_COLUMNS = {"source", "external_ref", "distance_meters"}
STARTED = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)


def utc(instant: datetime) -> str:
    return instant.isoformat().replace("+00:00", "Z")


def envelope(
    activity_id: UUID, user_id: UUID, *, version: int = 1, distance: int = 10234
) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "actividad.importada.v1",
        "occurred_at": utc(datetime.now(UTC).replace(microsecond=0)),
        "producer": "third-party-management",
        "aggregate_type": "external_activity",
        "aggregate_id": str(activity_id),
        "aggregate_version": version,
        "correlation_id": str(uuid.uuid4()),
        "causation_id": str(uuid.uuid4()),
        "schema_version": 1,
        "payload": {
            "activity_id": str(activity_id),
            "user_id": str(user_id),
            "provider": "STRAVA",
            "external_activity_id": "12345678901",
            "sport": "running",
            "name": "Actividad sintética",
            "started_at": utc(STARTED),
            "ended_at": utc(STARTED + timedelta(seconds=3725)),
            "time_zone": "America/Bogota",
            "duration_seconds": 3725,
            "moving_seconds": 3600,
            "distance_meters": distance,
            "elevation_gain_meters": 85,
            "mapping_version": "strava-activity.v1",
            "version": version,
        },
    }


@dataclass
class Message:
    body: dict[str, Any]
    message_id: str = "m-1"
    delivery_attempt: int | None = 1
    acked: bool = False
    nacked: bool = False

    @property
    def data(self) -> bytes:
        return json.dumps(self.body).encode()

    def ack(self) -> None:
        self.acked = True

    def nack(self) -> None:
        self.nacked = True


class SessionHistorySourceMigrationTest(unittest.TestCase):
    def columns(self, url: str) -> set[str]:
        engine = create_engine(url, poolclass=pool.NullPool)
        try:
            return {c["name"] for c in inspect(engine).get_columns("training_session_history")}
        finally:
            engine.dispose()

    def test_migrates_from_empty_database_and_back(self) -> None:
        with temporary_database("training_management_hu031_migration") as url:
            upgrade(READ_HISTORY, url, PREVIOUS)
            engine = create_engine(url, poolclass=pool.NullPool)
            self.addCleanup(engine.dispose)
            session_id = uuid.uuid4()
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO training_session_history (session_id, athlete_id, plan_id,"
                        " sport, started_at, ended_at, duration_seconds, version, projected_at)"
                        " VALUES (:id, :athlete, NULL, 'running', now() - interval '1 hour',"
                        " now(), 3600, 2, now())"
                    ),
                    {"id": session_id, "athlete": uuid.uuid4()},
                )

            upgrade(READ_HISTORY, url)
            with engine.connect() as connection:
                existing = connection.execute(
                    text(
                        "SELECT source, external_ref, distance_meters"
                        " FROM training_session_history WHERE session_id = :id"
                    ),
                    {"id": session_id},
                ).one()

            self.assertEqual(current_revision(url), HEAD)
            self.assertLessEqual(NEW_COLUMNS, self.columns(url))
            # La sesión de HU032 existente queda como `SPORTAPP`, sin referencia ni distancia.
            self.assertEqual(tuple(existing), ("SPORTAPP", None, None))

            downgrade(READ_HISTORY, url, PREVIOUS)
            self.assertEqual(current_revision(url), PREVIOUS)
            self.assertFalse(NEW_COLUMNS & self.columns(url))

            upgrade(READ_HISTORY, url)
            self.assertEqual(current_revision(url), HEAD)


class ExternalActivityConsumerPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: TrainingDatabases = self.enterContext(training_databases("hu031"))
        self.project = ProjectExternalActivity(
            unit_of_work=lambda: SqlAlchemyExternalActivityUnitOfWork(self.db.reads),
            clock=SystemClock(),
        )
        self.handler = ExternalActivityMessageHandler(self.project)
        self.athlete_id = uuid.uuid4()
        self.activity_id = uuid.uuid5(uuid.NAMESPACE_URL, f"strava:{uuid.uuid4().int}")

    def deliver(self, body: dict[str, Any]) -> Message:
        message = Message(body)
        self.handler(message)
        return message

    def row(self, session_id: UUID | None = None) -> TrainingSessionHistoryRow | None:
        with self.db.reads() as session:
            return session.get(TrainingSessionHistoryRow, session_id or self.activity_id)

    def test_new_activity_is_projected_as_strava_and_acked(self) -> None:
        message = self.deliver(envelope(self.activity_id, self.athlete_id))

        row = self.row()
        self.assertTrue(message.acked)
        assert row is not None
        self.assertEqual(
            (row.source, row.external_ref, row.distance_meters, row.plan_id, row.version),
            ("STRAVA", self.activity_id, 10234, None, 1),
        )
        self.assertEqual(
            (row.athlete_id, row.sport, row.duration_seconds),
            (
                self.athlete_id,
                "running",
                3725,
            ),
        )
        self.assertEqual(self.db.count(ProcessedEventRow, read=True), 1)

    def test_duplicate_event_id_has_no_effect(self) -> None:
        body = envelope(self.activity_id, self.athlete_id)
        self.deliver(body)
        before = self.row()

        message = self.deliver(body)

        self.assertTrue(message.acked)
        after = self.row()
        assert before is not None
        assert after is not None
        self.assertEqual(after.projected_at, before.projected_at)
        self.assertEqual(self.db.count(ProcessedEventRow, read=True), 1)

    def test_higher_version_replaces_and_lower_or_equal_is_ignored(self) -> None:
        self.deliver(envelope(self.activity_id, self.athlete_id, version=2, distance=10000))

        stale = self.deliver(envelope(self.activity_id, self.athlete_id, version=1, distance=1))
        same = self.deliver(envelope(self.activity_id, self.athlete_id, version=2, distance=2))
        newer = self.deliver(envelope(self.activity_id, self.athlete_id, version=3, distance=12000))

        self.assertTrue(stale.acked and same.acked and newer.acked)
        row = self.row()
        assert row is not None
        self.assertEqual((row.version, row.distance_meters), (3, 12000))
        self.assertEqual(self.db.count(ProcessedEventRow, read=True), 4)
        self.assertEqual(self.db.count(TrainingSessionHistoryRow, read=True), 1)

    def test_concurrent_deliveries_of_the_same_event_apply_once(self) -> None:
        body = envelope(self.activity_id, self.athlete_id)
        barrier = threading.Barrier(4)
        messages = [Message(body, message_id=f"m-{index}") for index in range(4)]

        def deliver(message: Message) -> None:
            barrier.wait()
            self.handler(message)

        threads = [threading.Thread(target=deliver, args=(m,)) for m in messages]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertTrue(all(m.acked for m in messages))
        self.assertEqual(self.db.count(ProcessedEventRow, read=True), 1)
        self.assertEqual(self.db.count(TrainingSessionHistoryRow, read=True), 1)

    def test_concurrent_versions_of_the_same_activity_keep_the_highest(self) -> None:
        bodies = [
            envelope(self.activity_id, self.athlete_id, version=version, distance=version * 100)
            for version in (1, 2, 3, 4)
        ]
        barrier = threading.Barrier(len(bodies))
        messages = [Message(body) for body in bodies]

        def deliver(message: Message) -> None:
            barrier.wait()
            self.handler(message)

        threads = [threading.Thread(target=deliver, args=(m,)) for m in messages]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        row = self.row()
        assert row is not None
        self.assertTrue(all(m.acked for m in messages))
        self.assertEqual((row.version, row.distance_meters), (4, 400))

    def test_never_overwrites_a_sportapp_session_with_the_same_id(self) -> None:
        with self.db.reads() as session, session.begin():
            session.execute(
                insert(TrainingSessionHistoryRow).values(
                    session_id=self.activity_id,
                    athlete_id=self.athlete_id,
                    plan_id=None,
                    sport="cycling",
                    started_at=STARTED,
                    ended_at=STARTED + timedelta(hours=1),
                    duration_seconds=3600,
                    version=1,
                    projected_at=STARTED,
                )
            )

        message = self.deliver(envelope(self.activity_id, self.athlete_id, version=9))

        row = self.row()
        assert row is not None
        self.assertTrue(message.acked)
        self.assertEqual(
            (row.source, row.sport, row.distance_meters), ("SPORTAPP", "cycling", None)
        )

    def test_invalid_event_is_nacked_without_writes(self) -> None:
        body = envelope(self.activity_id, self.athlete_id)
        body["payload"]["raw"] = {"id": 1}

        message = self.deliver(body)

        self.assertTrue(message.nacked)
        self.assertIsNone(self.row())
        with self.db.reads() as session:
            self.assertIsNone(session.scalar(select(ProcessedEventRow.event_id)))

    def test_history_lists_the_imported_activity_with_its_source(self) -> None:
        """HU011 + HU031: la actividad importada aparece en el historial con `STRAVA`."""
        self.deliver(envelope(self.activity_id, self.athlete_id))

        page = self.db.history().own(
            Caller(self.athlete_id, "athlete"),
            HistoryFilters(date_from=None, date_to=None, sport=None, limit=20, cursor=None),
        )

        [item] = page.items
        self.assertEqual((item.session_id, item.source), (self.activity_id, "STRAVA"))


if __name__ == "__main__":
    unittest.main()
