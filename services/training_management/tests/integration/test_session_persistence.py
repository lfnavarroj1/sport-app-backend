"""HU032 contra PostgreSQL real: transacciones, restricción de una sesión activa, carreras,
plan propio, outbox y proyección con reconstrucción (CA1-CA5, CA8, CA9)."""

import threading
import unittest
from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import DataError
from support import (
    AlwaysCapable,
    TrainingDatabases,
    athlete_and_professional,
    finish_command,
    plan_command,
    start_command,
    training_databases,
    utc_seconds,
)

from training_management.application.commands.training_sessions import StartTrainingSession
from training_management.application.errors import (
    ActiveSessionExists,
    TrainingPlanNotFound,
    TrainingSessionAlreadyFinished,
)
from training_management.application.messages import parse_training_event
from training_management.application.ports import OutboxMessage
from training_management.domain.plans.model import Sport
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.persistence_read.models import (
    ActiveTrainingSessionRow,
    TrainingSessionHistoryRow,
)
from training_management.infrastructure.persistence_write.models import (
    CommandIdempotencyRow,
    OutboxEventRow,
    TrainingSessionRow,
)
from training_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from training_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemySessionUnitOfWork,
)
from training_management.workers.outbox_publisher import build_envelope


class BrokenOutboxUnitOfWork(SqlAlchemySessionUnitOfWork):
    def add_outbox(self, message: OutboxMessage) -> None:
        super().add_outbox(OutboxMessage(**{**message.__dict__, "aggregate_type": "x" * 65}))


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


class SessionPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: TrainingDatabases = self.enterContext(training_databases("hu032"))
        self.athlete, self.professional = athlete_and_professional()

    def published(self) -> list[dict[str, Any]]:
        store = SqlAlchemyOutboxStore(self.db.writes)
        claimed = store.claim_batch("w-test", 100, 30)
        for event in claimed:
            store.mark_published(event.event_id, "w-test")
        return [build_envelope(event) for event in claimed]

    def test_start_and_finish_commit_session_event_and_key_together(self) -> None:
        """CA1 y CA2."""
        started = self.db.start_session()(self.athlete, start_command())
        finished = self.db.finish_session()(
            self.athlete, finish_command(started.session.session_id)
        )

        with self.db.writes() as session:
            row = session.get(TrainingSessionRow, started.session.session_id)
            events = list(session.scalars(select(OutboxEventRow.event_type)))
            commands = list(session.scalars(select(CommandIdempotencyRow.operation)))
        assert row is not None
        self.assertEqual((row.status, row.version), ("FINISHED", 2))
        self.assertEqual(row.duration_seconds, finished.session.duration_seconds)
        self.assertEqual(
            sorted(events), ["entrenamiento.finalizado.v1", "entrenamiento.iniciado.v1"]
        )
        self.assertEqual(sorted(commands), ["finish_training_session", "start_training_session"])

    def test_outbox_failure_leaves_nothing(self) -> None:
        start = StartTrainingSession(
            unit_of_work=lambda: BrokenOutboxUnitOfWork(self.db.writes),
            capabilities=AlwaysCapable(),
            clock=SystemClock(),
        )

        with self.assertRaises(DataError):
            start(self.athlete, start_command())

        self.assertEqual(
            [self.db.count(m) for m in (TrainingSessionRow, OutboxEventRow, CommandIdempotencyRow)],
            [0, 0, 0],
        )

    def test_eight_concurrent_starts_create_one_active_session(self) -> None:
        """CA5: la restricción única parcial serializa la carrera."""
        start = self.db.start_session()

        results, errors = concurrently(8, lambda: start(self.athlete, start_command()))

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 7)
        self.assertTrue(all(isinstance(error, ActiveSessionExists) for error in errors), errors)
        self.assertEqual((self.db.count(TrainingSessionRow), self.db.count(OutboxEventRow)), (1, 1))

    def test_concurrent_retries_of_the_same_start_and_finish(self) -> None:
        """CA4: reintentos simultáneos con la misma clave → un efecto y un evento por comando."""
        session_id, key = uuid4(), uuid4()
        command = start_command(session_id, key=key)
        start, finish = self.db.start_session(), self.db.finish_session()

        started, start_errors = concurrently(6, lambda: start(self.athlete, command))
        ending = finish_command(session_id, key=uuid4())
        finished, finish_errors = concurrently(6, lambda: finish(self.athlete, ending))

        self.assertEqual((start_errors, finish_errors), ([], []))
        self.assertEqual(sum(not result.replayed for result in started), 1)
        self.assertEqual(sum(not result.replayed for result in finished), 1)
        self.assertEqual(self.db.count(OutboxEventRow), 2)

    def test_finish_conflicts(self) -> None:
        session = self.db.start_session()(self.athlete, start_command()).session
        self.db.finish_session()(self.athlete, finish_command(session.session_id))

        with self.assertRaises(TrainingSessionAlreadyFinished):
            self.db.finish_session()(
                self.athlete,
                finish_command(session.session_id, ended_at=utc_seconds(-timedelta(minutes=1))),
            )

        # Una sesión nueva se puede iniciar en cuanto la anterior terminó.
        self.db.start_session()(self.athlete, start_command())
        self.assertEqual(self.db.count(TrainingSessionRow), 2)

    def test_plan_must_be_own_and_include_the_sport(self) -> None:
        """CA9 con un plan real de HU010."""
        self.db.grant()(self.athlete, self.professional.user_id)
        plan = self.db.create_plan()(self.professional, plan_command(self.athlete.user_id)).plan
        stranger, _ = athlete_and_professional()

        with self.assertRaises(TrainingPlanNotFound):
            self.db.start_session()(
                self.athlete, start_command(plan_id=plan.plan_id, sport=Sport.SWIMMING.value)
            )
        with self.assertRaises(TrainingPlanNotFound):
            self.db.start_session()(stranger, start_command(plan_id=plan.plan_id))
        linked = self.db.start_session()(self.athlete, start_command(plan_id=plan.plan_id))

        self.assertEqual(linked.session.plan_id, plan.plan_id)

    def test_projection_builds_active_session_then_history_and_can_be_rebuilt(self) -> None:
        """CA8: activa → historial una sola vez, con duplicados, y reconstrucción idéntica."""
        project, reader = self.db.project(), self.db.active_reader()
        session_id = self.db.start_session()(self.athlete, start_command()).session.session_id
        [started] = self.published()

        project(parse_training_event(started))
        project(parse_training_event(started))
        active = reader.current(self.athlete.user_id)

        assert active is not None
        self.assertEqual((active.session_id, active.version), (session_id, 1))

        self.db.finish_session()(self.athlete, finish_command(session_id))
        [finished] = self.published()
        for _ in range(2):
            project(parse_training_event(finished))

        self.assertIsNone(reader.current(self.athlete.user_id))
        self.assertEqual(self.db.count(TrainingSessionHistoryRow, read=True), 1)
        snapshot = self.history_rows()

        with self.db.reads() as session, session.begin():
            session.execute(
                text("TRUNCATE training_session_history, active_training_session, processed_event")
            )
        for envelope in (finished, started):  # fuera de orden a propósito
            project(parse_training_event(envelope))

        self.assertEqual(self.history_rows(), snapshot)
        self.assertEqual(self.db.count(ActiveTrainingSessionRow, read=True), 0)

    def history_rows(self) -> list[tuple[Any, ...]]:
        with self.db.reads() as session:
            rows = session.scalars(select(TrainingSessionHistoryRow))
            return [
                (row.session_id, row.athlete_id, row.duration_seconds, row.version) for row in rows
            ]


if __name__ == "__main__":
    unittest.main()
