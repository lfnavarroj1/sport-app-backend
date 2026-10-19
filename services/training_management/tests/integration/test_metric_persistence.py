"""HU034 contra PostgreSQL real: transacción de cálculo, entradas, outbox e idempotencia,
carreras, proyección con duplicados, conflicto y reconstrucción, e historial keyset
(ADR-030, CA1-CA5)."""

import threading
import unittest
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import DataError
from support import (
    AlwaysCapable,
    TrainingDatabases,
    athlete_and_professional,
    metric_command,
    training_databases,
    utc_seconds,
)

from training_management.application.commands.calculate_training_metric import (
    CalculateTrainingMetric,
    CalculationOutcome,
)
from training_management.application.errors import CalculationIdReused, IdempotencyKeyReused
from training_management.application.messages import parse_training_event
from training_management.application.ports import Caller, OutboxMessage
from training_management.application.projections.metric_projection import MetricResultConflict
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.application.queries.training_metrics import MetricResultFilters
from training_management.domain.metrics.catalog import TRAINING_METRICS_CATALOG
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.persistence_read.models import TrainingMetricResultRow
from training_management.infrastructure.persistence_write.models import (
    CommandIdempotencyRow,
    OutboxEventRow,
    TrainingMetricCalculationRow,
    TrainingMetricInputRow,
)
from training_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from training_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyMetricUnitOfWork,
)
from training_management.workers.outbox_publisher import build_envelope


class BrokenOutboxUnitOfWork(SqlAlchemyMetricUnitOfWork):
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


class MetricPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: TrainingDatabases = self.enterContext(training_databases("hu034"))
        self.athlete, _ = athlete_and_professional()
        self.calculate = self.db.calculate_metric()

    def published(self) -> list[dict[str, Any]]:
        store = SqlAlchemyOutboxStore(self.db.writes)
        claimed = store.claim_batch("w-test", 100, 30)
        for event in claimed:
            store.mark_published(event.event_id, "w-test")
        return [build_envelope(event) for event in claimed]

    def counts(self) -> list[int]:
        return [
            self.db.count(model)
            for model in (
                TrainingMetricCalculationRow,
                TrainingMetricInputRow,
                OutboxEventRow,
                CommandIdempotencyRow,
            )
        ]

    def test_calculation_inputs_event_and_key_commit_together(self) -> None:
        """CA1 y CA5: valores con escala explícita y el evento con el estado completo."""
        key = uuid4()
        result = self.calculate(self.athlete, metric_command(key=key))

        with self.db.writes() as session:
            row = session.get(TrainingMetricCalculationRow, result.calculation.calculation_id)
            inputs = list(session.scalars(select(TrainingMetricInputRow)))
            command = session.scalar(select(CommandIdempotencyRow))
        assert row is not None
        assert command is not None
        self.assertEqual(
            (row.result_value, row.result_scale, row.result_unit), (Decimal("238.0000"), 0, "W")
        )
        self.assertEqual((row.status, row.version), ("CONFIRMED", 1))
        self.assertEqual(row.data_fingerprint, result.calculation.data.fingerprint)
        self.assertEqual(
            [(item.code, item.value, item.unit) for item in inputs],
            [("mean_power_watts", Decimal("250.0000"), "W")],
        )
        self.assertEqual((command.operation, command.key), ("calculate_training_metric", key))
        [envelope] = self.published()
        self.assertEqual(
            (envelope["event_type"], envelope["aggregate_type"], envelope["aggregate_version"]),
            ("indicador.calculado.v1", "training_metric_calculation", 1),
        )
        self.assertEqual(envelope["aggregate_id"], str(result.calculation.calculation_id))
        self.assertEqual(envelope["causation_id"], str(key))
        self.assertEqual(envelope["payload"]["result"], {"value": 238, "unit": "W", "scale": 0})
        self.assertEqual(
            envelope["payload"]["inputs"], [{"code": "mean_power_watts", "value": 250, "unit": "W"}]
        )

    def test_outbox_failure_leaves_nothing(self) -> None:
        calculate = CalculateTrainingMetric(
            unit_of_work=lambda: BrokenOutboxUnitOfWork(self.db.writes),
            capabilities=AlwaysCapable(),
            catalog=TRAINING_METRICS_CATALOG,
            clock=SystemClock(),
        )

        with self.assertRaises(DataError):
            calculate(self.athlete, metric_command())

        self.assertEqual(self.counts(), [0, 0, 0, 0])

    def test_eight_identical_concurrent_requests_create_one_calculation_and_event(self) -> None:
        """CA4: misma clave y payload en paralelo → un cálculo, un evento, todos con el mismo
        resultado."""
        command = metric_command(key=uuid4())

        results, errors = concurrently(8, lambda: self.calculate(self.athlete, command))

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(sum(result.outcome is CalculationOutcome.CREATED for result in results), 1)
        self.assertEqual({result.calculation for result in results}, {results[0].calculation})
        self.assertEqual(self.counts(), [1, 1, 1, 1])

    def test_concurrent_same_calculation_id_with_different_keys(self) -> None:
        calculation_id = uuid4()
        measured_at = utc_seconds(-timedelta(hours=2))

        results, errors = concurrently(
            6,
            lambda: self.calculate(
                self.athlete, metric_command(calculation_id=calculation_id, measured_at=measured_at)
            ),
        )

        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(result.outcome.value for result in results),
            ["calculated", *["existing"] * 5],
        )
        self.assertEqual(self.counts()[:3], [1, 1, 1])

    def test_calculation_id_and_key_reuse(self) -> None:
        """CA4: mismos datos → existente; otros datos → `409`; otra clave usada → `409`."""
        calculation_id, key = uuid4(), uuid4()
        measured_at = utc_seconds(-timedelta(hours=3))
        first = self.calculate(
            self.athlete,
            metric_command(key=key, calculation_id=calculation_id, measured_at=measured_at),
        )

        again = self.calculate(
            self.athlete, metric_command(calculation_id=calculation_id, measured_at=measured_at)
        )
        replay = self.calculate(
            self.athlete,
            metric_command(key=key, calculation_id=calculation_id, measured_at=measured_at),
        )
        with self.assertRaises(CalculationIdReused):
            self.calculate(self.athlete, metric_command(calculation_id=calculation_id, watts=300))
        with self.assertRaises(CalculationIdReused):
            self.calculate(
                Caller(uuid4(), "athlete"),
                metric_command(calculation_id=calculation_id, measured_at=measured_at),
            )
        with self.assertRaises(IdempotencyKeyReused):
            self.calculate(self.athlete, metric_command(key=key))

        self.assertEqual(again.outcome, CalculationOutcome.EXISTING)
        self.assertEqual(again.calculation, first.calculation)
        self.assertEqual(replay.outcome, CalculationOutcome.REPLAYED)
        self.assertEqual(replay.calculation, first.calculation)
        self.assertEqual(self.counts(), [1, 1, 1, 1])

    def test_projection_applies_once_detects_conflicts_and_rebuilds(self) -> None:
        """CA5: duplicados sin efecto, conflicto sin cambios y reconstrucción idéntica."""
        project = self.db.project()
        for minutes in (10, 20):
            self.calculate(
                self.athlete, metric_command(measured_at=utc_seconds(-timedelta(minutes=minutes)))
            )
        envelopes = self.published()

        outcomes = [project(parse_training_event(item)) for item in [*envelopes, *envelopes]]
        snapshot = self.result_rows()

        self.assertEqual(
            outcomes, [ProjectionOutcome.PROJECTED] * 2 + [ProjectionOutcome.DUPLICATE] * 2
        )
        self.assertEqual(len(snapshot), 2)

        tampered = {**envelopes[0], "event_id": str(uuid4())}
        tampered["payload"] = {
            **tampered["payload"],
            "result": {"value": 239, "unit": "W", "scale": 0},
        }
        with (
            self.assertLogs("sportapp.projector", level="WARNING"),
            self.assertRaises(MetricResultConflict),
        ):
            project(parse_training_event(tampered))
        republished = {**envelopes[0], "event_id": str(uuid4())}
        self.assertEqual(project(parse_training_event(republished)), ProjectionOutcome.STALE)
        self.assertEqual(self.result_rows(), snapshot)

        with self.db.reads() as session, session.begin():
            session.execute(text("TRUNCATE training_metric_result, processed_event"))
        for envelope in reversed(envelopes):
            project(parse_training_event(envelope))

        self.assertEqual(self.result_rows(), snapshot)

    def test_history_is_own_ordered_and_paginated_without_losses(self) -> None:
        project, results = self.db.project(), self.db.metric_results()
        other, _ = athlete_and_professional()
        measured = [utc_seconds(-timedelta(minutes=minutes)) for minutes in (5, 5, 5, 15, 25)]
        expected = [
            self.calculate(self.athlete, metric_command(measured_at=value)).calculation
            for value in measured
        ]
        self.calculate(other, metric_command())
        for envelope in self.published():
            project(parse_training_event(envelope))
        expected.sort(key=lambda item: (item.measured_at, item.calculation_id), reverse=True)

        seen: list[UUID] = []
        cursor = None
        while True:
            page = results(self.athlete, MetricResultFilters(limit=2, cursor=cursor))
            seen.extend(item.calculation_id for item in page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        projected = results(self.athlete, MetricResultFilters(limit=1)).items[0]

        self.assertEqual(seen, [item.calculation_id for item in expected])
        self.assertEqual(projected, expected[0])
        self.assertEqual(
            results(self.athlete, MetricResultFilters(metric_code="VO2MAX_ESTIMATED")).items, ()
        )

    def result_rows(self) -> list[tuple[Any, ...]]:
        with self.db.reads() as session:
            rows = session.scalars(
                select(TrainingMetricResultRow).order_by(TrainingMetricResultRow.calculation_id)
            )
            return [
                (
                    row.calculation_id,
                    row.athlete_id,
                    row.metric_code,
                    row.method_code,
                    row.method_version,
                    row.measured_at,
                    row.calculated_at,
                    row.inputs,
                    row.result_value,
                    row.result_scale,
                    row.status,
                    row.disclaimer_key,
                    row.version,
                )
                for row in rows
            ]


if __name__ == "__main__":
    unittest.main()
