"""HU042 contra PostgreSQL real: transacción de reporte, outbox e idempotencia, sesión propia,
carreras, migraciones y proyección con duplicados y reconstrucción (ADR-037, CA1-CA4)."""

import threading
import unittest
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, select, text
from sqlalchemy.exc import DataError, IntegrityError
from support import TrainingDatabases, start_command, training_databases, utc_seconds

from training_management.application.commands.report_health import (
    ReportHealth,
    ReportHealthCommand,
    ReportOutcome,
)
from training_management.application.errors import (
    IdempotencyKeyReused,
    ReportIdReused,
    TrainingSessionNotFound,
)
from training_management.application.messages import parse_training_event
from training_management.application.ports import Caller, OutboxMessage
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.persistence_read.models import (
    HealthReportViewRow,
    ProcessedEventRow,
)
from training_management.infrastructure.persistence_write.models import (
    CommandIdempotencyRow,
    HealthReportRow,
    OutboxEventRow,
)
from training_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from training_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyHealthReportUnitOfWork,
)
from training_management.workers.outbox_publisher import build_envelope

CONSENT = "local-dev"


class BrokenOutboxUnitOfWork(SqlAlchemyHealthReportUnitOfWork):
    def add_outbox(self, message: OutboxMessage) -> None:
        super().add_outbox(OutboxMessage(**{**message.__dict__, "aggregate_type": "x" * 65}))


def body(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "report_id": str(uuid4()),
        "type": "INJURY",
        "body_area": "KNEE",
        "severity": "MODERATE",
        "occurred_at": utc_seconds(-timedelta(hours=2)),
        "expected_recovery_days": 7,
        "consent": {"version": CONSENT},
    }
    values.update(overrides)
    return values


def command(document: dict[str, Any], key: UUID | None = None) -> ReportHealthCommand:
    return ReportHealthCommand(
        body=document, idempotency_key=str(key or uuid4()), correlation_id=uuid4()
    )


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


class HealthReportPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: TrainingDatabases = self.enterContext(training_databases("hu042"))
        self.athlete = Caller(uuid4(), "athlete")
        self.report = self.report_health()

    def report_health(self, unit_of_work: Any = SqlAlchemyHealthReportUnitOfWork) -> ReportHealth:
        return ReportHealth(
            unit_of_work=lambda: unit_of_work(self.db.writes),
            clock=SystemClock(),
            consent_version=CONSENT,
        )

    def counts(self) -> list[int]:
        return [
            self.db.count(model)
            for model in (HealthReportRow, OutboxEventRow, CommandIdempotencyRow)
        ]

    def published(self) -> list[dict[str, Any]]:
        store = SqlAlchemyOutboxStore(self.db.writes)
        claimed = store.claim_batch("w-test", 100, 30)
        for event in claimed:
            store.mark_published(event.event_id, "w-test")
        return [build_envelope(event) for event in claimed]

    def test_report_event_and_key_commit_together(self) -> None:
        """CA1: reporte `OPEN` versión 1 del deportista y su evento con el estado completo."""
        key = uuid4()
        document = body()

        result = self.report(self.athlete, command(document, key))

        with self.db.writes() as session:
            row = session.get(HealthReportRow, UUID(document["report_id"]))
            record = session.scalar(select(CommandIdempotencyRow))
        assert row is not None
        assert record is not None
        self.assertEqual(
            (row.athlete_id, row.status, row.version), (self.athlete.user_id, "OPEN", 1)
        )
        self.assertEqual((row.type, row.body_area, row.severity), ("INJURY", "KNEE", "MODERATE"))
        self.assertEqual((row.expected_recovery_days, row.health_consent_version), (7, CONSENT))
        self.assertIsNone(row.session_id)
        self.assertEqual((record.operation, record.key), ("report_health", key))
        self.assertEqual(record.expires_at - record.created_at, timedelta(days=30))
        [envelope] = self.published()
        self.assertEqual(
            (envelope["event_type"], envelope["aggregate_type"], envelope["aggregate_version"]),
            ("reporte_salud.registrado.v1", "health_report", 1),
        )
        self.assertEqual(envelope["aggregate_id"], document["report_id"])
        self.assertEqual(envelope["causation_id"], str(key))
        self.assertEqual(envelope["payload"]["athlete_id"], str(self.athlete.user_id))
        self.assertEqual(result.report.reported_at.microsecond, 0)

    def test_own_session_is_referenced_and_a_foreign_one_is_not_found(self) -> None:
        """CA2: `session_id` opcional de una sesión propia; ajena o inexistente → 404."""
        own = uuid4()
        self.db.start_session()(self.athlete, start_command(own))
        foreign = uuid4()
        self.db.start_session()(Caller(uuid4(), "athlete"), start_command(foreign))
        self.db.discard_pending_outbox()

        result = self.report(self.athlete, command(body(session_id=str(own))))
        for session_id in (foreign, uuid4()):
            with self.subTest(session_id=session_id), self.assertRaises(TrainingSessionNotFound):
                self.report(self.athlete, command(body(session_id=str(session_id))))

        self.assertEqual(result.report.session_id, own)
        self.assertEqual(self.counts(), [1, 3, 3])

    def test_outbox_failure_leaves_nothing(self) -> None:
        with self.assertRaises(DataError):
            self.report_health(BrokenOutboxUnitOfWork)(self.athlete, command(body()))

        self.assertEqual(self.counts(), [0, 0, 0])

    def test_eight_identical_concurrent_requests_create_one_report_and_event(self) -> None:
        """Ocho envíos concurrentes con la misma clave → un reporte y un evento."""
        same = command(body())

        results, errors = concurrently(8, lambda: self.report(self.athlete, same))

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(sum(result.outcome is ReportOutcome.CREATED for result in results), 1)
        self.assertEqual({result.report for result in results}, {results[0].report})
        self.assertEqual(self.counts(), [1, 1, 1])

    def test_concurrent_same_report_id_with_different_keys(self) -> None:
        document = body()

        results, errors = concurrently(6, lambda: self.report(self.athlete, command(document)))

        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(result.outcome.value for result in results), ["created", *["existing"] * 5]
        )
        self.assertEqual(self.counts()[:2], [1, 1])

    def test_report_id_and_key_reuse(self) -> None:
        """CA3."""
        document, key = body(), uuid4()
        first = self.report(self.athlete, command(document, key))

        replay = self.report(self.athlete, command(document, key))
        again = self.report(self.athlete, command(document))
        with self.assertRaises(ReportIdReused):
            self.report(self.athlete, command({**document, "severity": "HIGH"}))
        with self.assertRaises(ReportIdReused):
            self.report(Caller(uuid4(), "athlete"), command(document))
        with self.assertRaises(IdempotencyKeyReused):
            self.report(self.athlete, command(body(), key))

        self.assertEqual((replay.outcome, replay.report), (ReportOutcome.REPLAYED, first.report))
        self.assertEqual((again.outcome, again.report), (ReportOutcome.EXISTING, first.report))
        # El reporte existente con otra clave se devuelve sin registrar esa clave (como HU034).
        self.assertEqual(self.counts(), [1, 1, 1])

    def test_database_enforces_the_closed_catalogs(self) -> None:
        self.report(self.athlete, command(body()))
        for column, value in (
            ("body_area", "'TOE'"),
            ("severity", "'CRITICAL'"),
            ("status", "'CLOSED'"),
            ("expected_recovery_days", "366"),
        ):
            with (
                self.subTest(column=column),
                self.db.writes() as session,
                self.assertRaises(IntegrityError),
            ):
                session.execute(text(f"UPDATE health_report SET {column} = {value}"))  # noqa: S608
                session.commit()

    def test_projection_applies_once_and_rebuilds_identically(self) -> None:
        """CA4: entregas repetidas sin efecto y reconstrucción idéntica desde los eventos."""
        project = self.db.project()
        for hours in (3, 1, 2):
            self.report(
                self.athlete, command(body(occurred_at=utc_seconds(-timedelta(hours=hours))))
            )
        envelopes = self.published()

        outcomes = [project(parse_training_event(item)) for item in [*envelopes, *envelopes]]
        snapshot = self.view_rows()

        self.assertEqual(
            outcomes, [ProjectionOutcome.PROJECTED] * 3 + [ProjectionOutcome.DUPLICATE] * 3
        )
        self.assertEqual(len(snapshot), 3)
        self.assertEqual(self.db.count(ProcessedEventRow, read=True), 3)

        with self.db.reads() as session, session.begin():
            session.execute(delete(HealthReportViewRow))
            session.execute(delete(ProcessedEventRow))
        for item in reversed(envelopes):
            project(parse_training_event(item))

        self.assertEqual(self.view_rows(), snapshot)

    def test_view_order_index_exists(self) -> None:
        with self.db.reads() as session:
            definition = session.scalar(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'ix_health_report_view_athlete_order'"
                )
            )

        self.assertEqual(
            definition,
            "CREATE INDEX ix_health_report_view_athlete_order ON public.health_report_view "
            "USING btree (athlete_id, occurred_at DESC, report_id DESC)",
        )

    def view_rows(self) -> list[tuple[Any, ...]]:
        with self.db.reads() as session:
            rows = session.scalars(
                select(HealthReportViewRow).order_by(
                    HealthReportViewRow.occurred_at.desc(), HealthReportViewRow.report_id.desc()
                )
            )
            return [
                (
                    row.report_id,
                    row.athlete_id,
                    row.session_id,
                    row.type,
                    row.body_area,
                    row.severity,
                    row.occurred_at,
                    row.expected_recovery_days,
                    row.status,
                    row.version,
                    row.reported_at,
                )
                for row in rows
            ]


if __name__ == "__main__":
    unittest.main()
