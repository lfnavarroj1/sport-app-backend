"""HU010 con PostgreSQL real: transacción del plan, idempotencia, concurrencia,
outbox y proyección de lectura (requiere `TEST_DATABASE_URL`)."""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from support import athlete_and_professional, plan_command, training_databases

from training_management.application.errors import AthleteAuthorizationRequired
from training_management.application.messages import parse_training_event
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.infrastructure.persistence_read.models import (
    ProcessedEventRow,
    TrainingPlanViewRow,
)
from training_management.infrastructure.persistence_write.models import (
    IdempotencyKeyRow,
    OutboxEventRow,
    TrainingAuthorizationRow,
    TrainingPlanRow,
    TrainingPlanSessionRow,
)
from training_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from training_management.settings import OutboxPublisherSettings
from training_management.workers.outbox_publisher import OutboxRelay

CONCURRENCY = 8


def run_concurrently(task: Any, times: int = CONCURRENCY) -> list[Any]:
    barrier = threading.Barrier(times)

    def synchronized() -> Any:
        barrier.wait()
        try:
            return task()
        except Exception as error:
            return error

    with ThreadPoolExecutor(max_workers=times) as pool:
        return list(pool.map(lambda _: synchronized(), range(times)))


class PlanPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = self.enterContext(training_databases("hu010"))
        self.athlete, self.professional = athlete_and_professional()

    def test_plan_sessions_outbox_and_key_are_committed_together(self) -> None:
        """CA1, CA4 y CA5 en una sola transacción local."""
        self.db.grant()(self.athlete, self.professional.user_id)
        command = plan_command(self.athlete.user_id)

        result = self.db.create_plan()(self.professional, command)

        with self.db.writes() as session:
            plan = session.get(TrainingPlanRow, result.plan.plan_id)
            outbox = session.scalars(select(OutboxEventRow)).one()
            key = session.get(IdempotencyKeyRow, command.idempotency_key)
        assert plan is not None
        assert key is not None
        self.assertEqual(
            (plan.athlete_id, plan.professional_id),
            (self.athlete.user_id, self.professional.user_id),
        )
        self.assertEqual(self.db.count(TrainingPlanSessionRow), 2)
        self.assertEqual(
            (outbox.aggregate_id, outbox.event_type), (plan.plan_id, "plan.entrenamiento_creado.v1")
        )
        self.assertEqual(key.resource_id, plan.plan_id)

    def test_rejected_plan_leaves_nothing(self) -> None:
        """CA2: sin autorización no se escribe nada."""
        with self.assertRaises(AthleteAuthorizationRequired):
            self.db.create_plan()(self.professional, plan_command(self.athlete.user_id))

        for model in (TrainingPlanRow, TrainingPlanSessionRow, OutboxEventRow, IdempotencyKeyRow):
            self.assertEqual(self.db.count(model), 0, model.__tablename__)

    def test_concurrent_retries_create_a_single_plan(self) -> None:
        """CA5 con concurrencia real: 8 reintentos simultáneos con la misma clave."""
        self.db.grant()(self.athlete, self.professional.user_id)
        command = plan_command(self.athlete.user_id)
        create = self.db.create_plan()

        results = run_concurrently(lambda: create(self.professional, command))

        errors = [result for result in results if isinstance(result, Exception)]
        self.assertEqual(errors, [])
        self.assertEqual({result.plan.plan_id for result in results}, {results[0].plan.plan_id})
        self.assertEqual(self.db.count(TrainingPlanRow), 1)
        self.assertEqual(self.db.count(OutboxEventRow), 1)

    def test_concurrent_first_grants_create_one_authorization(self) -> None:
        grant = self.db.grant()

        results = run_concurrently(lambda: grant(self.athlete, self.professional.user_id))

        self.assertEqual([r for r in results if isinstance(r, Exception)], [])
        self.assertEqual(sum(result.created for result in results), 1)
        self.assertEqual(self.db.count(TrainingAuthorizationRow), 1)

    def test_revocation_blocks_later_plans(self) -> None:
        """CA2 con persistencia real: autorizar, revocar y reautorizar."""
        grant, revoke, create = self.db.grant(), self.db.revoke(), self.db.create_plan()
        grant(self.athlete, self.professional.user_id)
        revoke(self.athlete, self.professional.user_id)

        with self.assertRaises(AthleteAuthorizationRequired):
            create(self.professional, plan_command(self.athlete.user_id))
        grant(self.athlete, self.professional.user_id)
        create(self.professional, plan_command(self.athlete.user_id))

        with self.db.writes() as session:
            row = session.scalars(select(TrainingAuthorizationRow)).one()
        self.assertEqual((row.status, row.version), ("ACTIVE", 3))
        self.assertEqual(self.db.count(TrainingPlanRow), 1)

    def test_revocation_waits_for_an_in_flight_plan(self) -> None:
        """La autorización leída por el comando queda bloqueada hasta su COMMIT: una
        revocación concurrente espera y no puede intercalarse con la creación."""
        self.db.grant()(self.athlete, self.professional.user_id)
        revoked = threading.Event()
        with self.db.writes() as session, session.begin():
            session.execute(
                select(TrainingAuthorizationRow.status)
                .where(TrainingAuthorizationRow.athlete_id == self.athlete.user_id)
                .with_for_update(read=True)
            )
            worker = threading.Thread(
                target=lambda: (
                    self.db.revoke()(self.athlete, self.professional.user_id),
                    revoked.set(),
                )
            )
            worker.start()
            self.assertFalse(revoked.wait(1.0), "la revocación no esperó el bloqueo compartido")
        worker.join(timeout=10)

        self.assertTrue(revoked.is_set())


class OutboxAndProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = self.enterContext(training_databases("hu010_cqrs"))
        athlete, professional = athlete_and_professional()
        self.db.grant()(athlete, professional.user_id)
        self.created = self.db.create_plan()(professional, plan_command(athlete.user_id))

    def relay(self, publisher: Any) -> int:
        settings = OutboxPublisherSettings(
            app_env="test",  # type: ignore[arg-type]
            database_write_url=self.db.write_url,  # type: ignore[arg-type]
            pubsub_project_id="sportapp-test",
            worker_id="w-test",
        )
        return OutboxRelay(SqlAlchemyOutboxStore(self.db.writes), publisher, settings).run_once()

    def test_published_event_is_projected_once(self) -> None:
        """Publicación con ACK, proyección, entregas duplicadas y consulta (CA4)."""
        sent: list[tuple[dict[str, Any], str]] = []

        class Publisher:
            def publish(self, envelope: dict[str, Any], ordering_key: str) -> None:
                sent.append((envelope, ordering_key))

        self.assertEqual(self.relay(Publisher()), 1)
        self.assertEqual(self.relay(Publisher()), 0, "un evento publicado no se reclama otra vez")
        envelope, ordering_key = sent[0]
        self.assertEqual(ordering_key, str(self.created.plan.plan_id))

        event = parse_training_event(envelope)
        project = self.db.project()
        outcomes = run_concurrently(lambda: project(event), times=4)

        self.assertEqual(
            sorted(outcome.value for outcome in outcomes),
            ["duplicate", "duplicate", "duplicate", "projected"],
        )
        self.assertEqual(self.db.count(TrainingPlanViewRow, read=True), 1)
        self.assertEqual(self.db.count(ProcessedEventRow, read=True), 1)
        self.assertEqual(self.db.reader().get(self.created.plan.plan_id), self.created.plan)
        self.assertIsNone(self.db.reader().get(uuid4()))
        self.assertEqual(project(event), ProjectionOutcome.DUPLICATE)

    def test_failed_publication_stays_pending(self) -> None:
        class Failing:
            def publish(self, envelope: dict[str, Any], ordering_key: str) -> None:
                raise RuntimeError("sin ACK")

        with self.assertLogs("sportapp.outbox", level="WARNING"):
            self.assertEqual(self.relay(Failing()), 1)

        with self.db.writes() as session:
            row = session.scalars(select(OutboxEventRow)).one()
        self.assertIsNone(row.published_at)
        self.assertEqual((row.attempts, row.last_error), (1, "RuntimeError"))


if __name__ == "__main__":
    unittest.main()
