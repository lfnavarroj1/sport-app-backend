"""HU034 sin infraestructura: comando, consultas, evento y proyección (ADR-030)."""

import json
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fakes import NOW, FakeCapabilities, FixedClock, InMemoryProjectionUnitOfWork, InMemoryReadStore
from metric_fakes import (
    EXTENDED_TEST_CATALOG,
    METRICS_CAPABILITY,
    InMemoryMetricResultReader,
    InMemoryMetricStore,
    InMemoryMetricUnitOfWork,
    cooper_body,
    ftp_body,
    recovery_body,
)
from test_workers import Message

from training_management.application.commands.calculate_training_metric import (
    OPERATION,
    CalculateTrainingMetric,
    CalculateTrainingMetricCommand,
    CalculationOutcome,
)
from training_management.application.errors import (
    CalculationIdReused,
    CapabilitiesTimeout,
    CapabilitiesUnavailable,
    CapabilityRequired,
    IdempotencyKeyReused,
    PermissionRequired,
)
from training_management.application.messages import (
    InvalidEvent,
    MetricCalculatedEvent,
    parse_training_event,
)
from training_management.application.ports import Caller, MetricResultPosition
from training_management.application.projections.metric_projection import MetricResultConflict
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.application.projections.snapshot_projection import TrainingProjector
from training_management.application.queries.training_metrics import (
    GetMetricCatalog,
    InvalidMetricResultQuery,
    ListTrainingMetricResults,
    MetricResultFilters,
    encode_cursor,
)
from training_management.domain.metrics.catalog import TRAINING_METRICS_CATALOG, MetricCatalog
from training_management.domain.metrics.events import calculated_payload
from training_management.domain.metrics.model import (
    TrainingMetricCalculation,
    validate_calculation,
)
from training_management.domain.metrics.validation import InvalidMetricCalculation
from training_management.workers.read_projector import ProjectionMessageHandler


class SlowAffiliation(FakeCapabilities):
    def has_capability(self, user_id: Any, capability: str) -> bool:
        raise CapabilitiesTimeout()


def metric_envelope(calculation: TrainingMetricCalculation, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "event_type": "indicador.calculado.v1",
        "occurred_at": calculation.calculated_at.isoformat(),
        "producer": "training-management",
        "aggregate_type": "training_metric_calculation",
        "aggregate_id": str(calculation.calculation_id),
        "aggregate_version": 1,
        "correlation_id": str(uuid.uuid4()),
        "causation_id": str(uuid.uuid4()),
        "schema_version": 1,
        "payload": calculated_payload(calculation),
    }
    values.update(overrides)
    return values


class CommandCase(unittest.TestCase):
    catalog: MetricCatalog = TRAINING_METRICS_CATALOG

    def setUp(self) -> None:
        self.store = InMemoryMetricStore()
        self.clock = FixedClock()
        self.athlete = Caller(uuid4(), "athlete")
        self.capabilities = FakeCapabilities({self.athlete.user_id: {METRICS_CAPABILITY}})
        self.calculate = self.command(self.capabilities)

    def command(
        self, capabilities: FakeCapabilities, before_commit: Any = None
    ) -> CalculateTrainingMetric:
        return CalculateTrainingMetric(
            unit_of_work=lambda: InMemoryMetricUnitOfWork(self.store, before_commit),
            capabilities=capabilities,
            catalog=self.catalog,
            clock=self.clock,
        )

    def run_command(
        self, body: Any, key: UUID | str | None = None, caller: Caller | None = None
    ) -> Any:
        raw_key = str(key) if key is not None else str(uuid4())
        return self.calculate(
            caller or self.athlete,
            CalculateTrainingMetricCommand(body=body, idempotency_key=raw_key),
        )


class CalculateTrainingMetricTest(CommandCase):
    def test_calculation_event_and_key_are_committed_together(self) -> None:
        """CA1, CA2 y CA5: resultado confirmado, evento completo y clave idempotente."""
        body, key = ftp_body(), uuid4()

        result = self.run_command(body, key)

        calculation = result.calculation
        self.assertEqual(result.outcome, CalculationOutcome.CREATED)
        self.assertEqual((calculation.result.value, calculation.result.scale), (238, 0))
        self.assertEqual(calculation.athlete_id, self.athlete.user_id)
        self.assertEqual(list(self.store.calculations), [UUID(body["calculation_id"])])
        [message] = self.store.outbox
        self.assertEqual(
            (message.event_type, message.aggregate_type, message.aggregate_version),
            ("indicador.calculado.v1", "training_metric_calculation", 1),
        )
        self.assertEqual(message.aggregate_id, calculation.calculation_id)
        self.assertEqual(message.causation_id, str(key))
        self.assertEqual(message.payload["result"], {"value": 238, "unit": "W", "scale": 0})
        [record] = self.store.commands.values()
        self.assertEqual((record.operation, record.key), (OPERATION, key))
        self.assertEqual(record.expires_at - record.created_at, timedelta(days=30))
        self.assertEqual(self.capabilities.calls, [(self.athlete.user_id, METRICS_CAPABILITY)])

    def test_same_key_and_payload_returns_the_original_result(self) -> None:
        """CA4: misma clave y payload normalizado → resultado original, sin duplicar."""
        body, key = cooper_body(), uuid4()
        first = self.run_command(body, key)
        self.clock.advance(timedelta(minutes=3))

        replay = self.run_command(
            {**body, "inputs": [dict(reversed(body["inputs"][0].items()))]}, key
        )

        self.assertEqual(replay.outcome, CalculationOutcome.REPLAYED)
        self.assertEqual(replay.calculation, first.calculation)
        self.assertEqual((len(self.store.calculations), len(self.store.outbox)), (1, 1))

    def test_same_key_with_other_payload_is_rejected(self) -> None:
        body, key = ftp_body(), uuid4()
        self.run_command(body, key)

        with self.assertRaises(IdempotencyKeyReused):
            self.run_command(
                {**body, "inputs": [{"code": "mean_power_watts", "value": 251, "unit": "W"}]}, key
            )
        with self.assertRaises(IdempotencyKeyReused):
            self.run_command(ftp_body(), key)
        self.assertEqual(len(self.store.outbox), 1)

    def test_keys_are_isolated_by_athlete_and_expire_after_30_days(self) -> None:
        key = uuid4()
        self.run_command(ftp_body(), key)
        other = Caller(uuid4(), "athlete")
        self.capabilities.granted[other.user_id] = {METRICS_CAPABILITY}

        self.run_command(ftp_body(), key, caller=other)
        self.clock.advance(timedelta(days=30))
        later = self.run_command(ftp_body(measured_at="2026-11-11T11:30:00Z"), key)

        self.assertEqual(later.outcome, CalculationOutcome.CREATED)
        self.assertEqual(len(self.store.outbox), 3)

    def test_same_calculation_id_with_same_data_returns_existing_without_event(self) -> None:
        """CA4: mismo `calculation_id` y datos con otra clave → existente (`200`)."""
        body = ftp_body()
        first = self.run_command(body)

        again = self.run_command(body, uuid4())

        self.assertEqual(again.outcome, CalculationOutcome.EXISTING)
        self.assertEqual(again.calculation, first.calculation)
        self.assertEqual((len(self.store.outbox), len(self.store.commands)), (1, 1))

    def test_same_calculation_id_with_other_data_or_athlete_is_rejected(self) -> None:
        body = ftp_body()
        self.run_command(body)
        other = Caller(uuid4(), "athlete")
        self.capabilities.granted[other.user_id] = {METRICS_CAPABILITY}

        for changed, caller in (
            ({**body, "measured_at": "2026-10-12T11:31:00Z"}, self.athlete),
            (
                {**body, "inputs": [{"code": "mean_power_watts", "value": 260, "unit": "W"}]},
                self.athlete,
            ),
            (body, other),
        ):
            with self.subTest(caller=caller.user_id), self.assertRaises(CalculationIdReused):
                self.run_command(changed, caller=caller)
        self.assertEqual(len(self.store.outbox), 1)

    def test_concurrent_identical_requests_return_the_winner(self) -> None:
        """Carrera con la misma clave: quien pierde devuelve lo que confirmó el ganador."""
        body, key = ftp_body(), uuid4()
        winner = self.command(self.capabilities)
        loser = self.command(
            self.capabilities,
            before_commit=lambda: winner(
                self.athlete, CalculateTrainingMetricCommand(body=body, idempotency_key=str(key))
            ),
        )

        result = loser(self.athlete, CalculateTrainingMetricCommand(body, str(key)))

        self.assertEqual(result.outcome, CalculationOutcome.REPLAYED)
        self.assertEqual((len(self.store.calculations), len(self.store.outbox)), (1, 1))

    def test_concurrent_same_calculation_id_with_other_key(self) -> None:
        body = ftp_body()
        winner = self.command(self.capabilities)
        loser = self.command(
            self.capabilities,
            before_commit=lambda: winner(
                self.athlete, CalculateTrainingMetricCommand(body, str(uuid4()))
            ),
        )

        result = loser(self.athlete, CalculateTrainingMetricCommand(body, str(uuid4())))

        self.assertEqual(result.outcome, CalculationOutcome.EXISTING)
        self.assertEqual(len(self.store.outbox), 1)

    def test_invalid_requests_persist_nothing(self) -> None:
        """CA3: sin resultado, clave ni evento parcial."""
        for body, key in (
            (ftp_body(inputs=[]), str(uuid4())),
            (ftp_body(athlete_id=str(uuid4())), str(uuid4())),
            (ftp_body(), None),
            (ftp_body(), "not-a-uuid"),
        ):
            with self.subTest(key=key), self.assertRaises(InvalidMetricCalculation):
                self.calculate(self.athlete, CalculateTrainingMetricCommand(body, key))
        self.assertEqual(
            (self.store.calculations, self.store.outbox, self.store.commands), ({}, [], {})
        )

    def test_missing_key_reports_body_rules_too(self) -> None:
        with self.assertRaises(InvalidMetricCalculation) as caught:
            self.calculate(
                self.athlete, CalculateTrainingMetricCommand(ftp_body(metric_code="X"), " ")
            )

        self.assertEqual(
            {(item.field, item.issue.value) for item in caught.exception.violations},
            {("Idempotency-Key", "required"), ("metric_code", "unknown_metric")},
        )

    def test_actor_and_capability_are_checked_before_anything_else(self) -> None:
        provider = Caller(uuid4(), "service_provider")
        without = Caller(uuid4(), "athlete")
        with self.assertRaises(PermissionRequired):
            self.run_command(ftp_body(), caller=provider)
        with self.assertRaises(CapabilityRequired):
            self.run_command(ftp_body(), caller=without)
        self.capabilities.unavailable = True
        with self.assertRaises(CapabilitiesUnavailable):
            self.run_command(ftp_body())
        self.calculate = self.command(SlowAffiliation())
        with self.assertRaises(CapabilitiesTimeout):
            self.run_command(ftp_body())
        self.assertEqual((self.store.calculations, self.store.outbox), ({}, []))

    def test_audit_has_no_inputs_result_or_token(self) -> None:
        body = ftp_body()
        with self.assertLogs("sportapp.audit", level="INFO") as logs:
            self.run_command(body)
            with self.assertRaises(InvalidMetricCalculation):
                self.run_command(
                    ftp_body(inputs=[{"code": "mean_power_watts", "value": 9999, "unit": "W"}])
                )

        fields = [record.__dict__["fields"] for record in logs.records]
        self.assertEqual(
            fields[0],
            {
                "result": "calculated",
                "calculation_id": body["calculation_id"],
                "metric_code": "FTP_ESTIMATED",
                "athlete_id": str(self.athlete.user_id),
            },
        )
        self.assertEqual(
            fields[1],
            {
                "result": "rejected",
                "reason": "validation_error",
                "athlete_id": str(self.athlete.user_id),
            },
        )
        self.assertEqual(
            {record.getMessage() for record in logs.records}, {"audit.training_metric_calculated"}
        )
        allowed = {"result", "reason", "calculation_id", "metric_code", "athlete_id"}
        for record_fields in fields:
            self.assertLessEqual(set(record_fields), allowed, "sin entradas, resultado ni token")
            self.assertTrue(
                set(record_fields.values()).isdisjoint({250, 238, 9999, "250", "238", "9999"})
            )
        rendered = json.dumps(fields)
        for sensitive in ("mean_power_watts", "Bearer"):
            self.assertNotIn(sensitive, rendered)


class ExtensibleCommandTest(CommandCase):
    """HA014: el mismo comando calcula un tercer método inyectado solo para la prueba."""

    catalog = EXTENDED_TEST_CATALOG

    def test_third_method_is_calculated_and_published(self) -> None:
        result = self.run_command(recovery_body())

        self.assertEqual(result.calculation.result.value, 32)
        self.assertEqual(
            self.store.outbox[0].payload["inputs"],
            [
                {"code": "peak_heart_rate_bpm", "value": 172, "unit": "bpm"},
                {"code": "recovery_heart_rate_bpm", "value": 140, "unit": "bpm"},
            ],
        )


def a_calculation(
    athlete_id: UUID, measured_at: datetime, metric: str = "FTP_ESTIMATED"
) -> TrainingMetricCalculation:
    body = (ftp_body if metric == "FTP_ESTIMATED" else cooper_body)(
        measured_at=measured_at.isoformat().replace("+00:00", "Z")
    )
    return TrainingMetricCalculation.calculate(
        athlete_id=athlete_id,
        validated=validate_calculation(body, TRAINING_METRICS_CATALOG, NOW),
        now=NOW,
    )


class MetricQueriesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.athlete = Caller(uuid4(), "athlete")
        self.capabilities = FakeCapabilities({self.athlete.user_id: {METRICS_CAPABILITY}})
        self.results: dict[UUID, TrainingMetricCalculation] = {}
        self.list = ListTrainingMetricResults(
            reader=InMemoryMetricResultReader(self.results),
            capabilities=self.capabilities,
            catalog=TRAINING_METRICS_CATALOG,
        )

    def add(
        self, athlete_id: UUID, minutes: int, metric: str = "FTP_ESTIMATED"
    ) -> TrainingMetricCalculation:
        calculation = a_calculation(athlete_id, NOW - timedelta(minutes=minutes), metric)
        self.results[calculation.calculation_id] = calculation
        return calculation

    def test_catalog_requires_athlete_and_capability(self) -> None:
        catalog = GetMetricCatalog(capabilities=self.capabilities, catalog=TRAINING_METRICS_CATALOG)

        self.assertIs(catalog(self.athlete), TRAINING_METRICS_CATALOG)
        with self.assertRaises(PermissionRequired):
            catalog(Caller(uuid4(), "service_provider"))
        with self.assertRaises(CapabilityRequired):
            catalog(Caller(uuid4(), "athlete"))

    def test_own_results_ordered_and_paginated_without_losses(self) -> None:
        """CA5: orden `measured_at` desc, `calculation_id` desc; keyset sin pérdidas."""
        expected = [self.add(self.athlete.user_id, minutes) for minutes in (5, 5, 5, 30, 60)]
        self.add(uuid4(), 1)
        expected.sort(key=lambda item: (item.measured_at, item.calculation_id), reverse=True)

        seen: list[UUID] = []
        cursor = None
        while True:
            page = self.list(self.athlete, MetricResultFilters(limit=2, cursor=cursor))
            seen.extend(item.calculation_id for item in page.items)
            # Un resultado nuevo más reciente no altera las páginas siguientes.
            self.add(self.athlete.user_id, 0)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        self.assertEqual(seen, [item.calculation_id for item in expected])

    def test_filter_by_metric_and_empty_collection(self) -> None:
        vo2 = self.add(self.athlete.user_id, 10, "VO2MAX_ESTIMATED")
        self.add(self.athlete.user_id, 20)

        other = uuid4()
        self.capabilities.granted[other] = {METRICS_CAPABILITY}

        page = self.list(self.athlete, MetricResultFilters(metric_code="VO2MAX_ESTIMATED"))
        nothing = self.list(Caller(other, "athlete"), MetricResultFilters())

        self.assertEqual([item.calculation_id for item in page.items], [vo2.calculation_id])
        self.assertEqual((nothing.items, nothing.next_cursor), ((), None))

    def test_invalid_filters(self) -> None:
        stale = encode_cursor(MetricResultPosition(datetime(2026, 1, 1), uuid4()))
        cases = {
            ("metric_code", "unknown_metric"): MetricResultFilters(metric_code="HR_RECOVERY_TEST"),
            ("limit", "out_of_range"): MetricResultFilters(limit=101),
            ("cursor", "invalid_cursor"): MetricResultFilters(cursor="%%%"),
        }
        for (field, issue), filters in cases.items():
            with self.subTest(field=field), self.assertRaises(InvalidMetricResultQuery) as caught:
                self.list(self.athlete, filters)
            self.assertEqual((caught.exception.field, caught.exception.issue), (field, issue))
        with self.assertRaises(InvalidMetricResultQuery):
            self.list(self.athlete, MetricResultFilters(cursor=stale))

    def test_results_require_athlete_and_capability(self) -> None:
        with self.assertRaises(PermissionRequired):
            self.list(Caller(uuid4(), "administrator"), MetricResultFilters())
        with self.assertRaises(CapabilityRequired):
            self.list(Caller(uuid4(), "athlete"), MetricResultFilters())


class MetricEventParsingTest(unittest.TestCase):
    def test_valid_event_rebuilds_the_calculation(self) -> None:
        calculation = a_calculation(uuid4(), NOW - timedelta(hours=1), "VO2MAX_ESTIMATED")

        event = parse_training_event(json.loads(json.dumps(metric_envelope(calculation))))

        self.assertIsInstance(event, MetricCalculatedEvent)
        assert isinstance(event, MetricCalculatedEvent)
        self.assertEqual(event.calculation, calculation)

    def test_contract_violations(self) -> None:
        calculation = a_calculation(uuid4(), NOW - timedelta(hours=1))
        payload = metric_envelope(calculation)["payload"]

        def with_payload(**changes: Any) -> dict[str, Any]:
            return metric_envelope(calculation, payload={**payload, **changes})

        cases: dict[str, Any] = {
            "aggregate_version_invalid": metric_envelope(calculation, aggregate_version=2),
            "unsupported_schema_version": metric_envelope(calculation, schema_version=2),
            "aggregate_id_mismatch": metric_envelope(calculation, aggregate_id=str(uuid4())),
            "unknown_status": with_payload(status="PENDING_SYNC"),
            "method_version_invalid": with_payload(method_version=0),
            "metric_code_invalid": with_payload(metric_code=""),
            "inputs_invalid": with_payload(inputs=[]),
            "result_invalid": with_payload(result={"value": 42.45, "unit": "W", "scale": 1}),
            "measured_at_without_timezone": with_payload(measured_at="2026-10-12T11:00:00"),
            "disclaimer_key_invalid": with_payload(disclaimer_key=None),
        }
        duplicated = [payload["inputs"][0], payload["inputs"][0]]
        more: dict[str, Any] = {
            "duplicated": with_payload(inputs=duplicated),
            "decimal input": with_payload(inputs=[{**payload["inputs"][0], "value": 250.5}]),
            "input no object": with_payload(inputs=["x"]),
            "result no object": with_payload(result=1),
            "boolean result": with_payload(result={"value": True, "unit": "W", "scale": 0}),
            "scale too large": with_payload(result={"value": 1, "unit": "W", "scale": 9}),
            "not finite": with_payload(result={"value": float("nan"), "unit": "W", "scale": 0}),
        }
        for reason, envelope in cases.items():
            with self.subTest(reason=reason), self.assertRaises(InvalidEvent) as caught:
                parse_training_event(envelope)
            self.assertEqual(caught.exception.reason, reason)
        for name, envelope in more.items():
            with self.subTest(name=name), self.assertRaises(InvalidEvent):
                parse_training_event(envelope)


class MetricProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.read = InMemoryReadStore()
        self.project = TrainingProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.read), clock=FixedClock()
        )

    def event(
        self, calculation: TrainingMetricCalculation, **overrides: Any
    ) -> MetricCalculatedEvent:
        parsed = parse_training_event(metric_envelope(calculation, **overrides))
        assert isinstance(parsed, MetricCalculatedEvent)
        return parsed

    def test_projects_once_and_ignores_duplicates(self) -> None:
        """CA5: el proyector aplica el resultado una vez aunque haya entregas duplicadas."""
        calculation = a_calculation(uuid4(), NOW - timedelta(hours=1))
        event = self.event(calculation)

        outcomes = [self.project(event), self.project(event), self.project(self.event(calculation))]

        self.assertEqual(
            outcomes,
            [ProjectionOutcome.PROJECTED, ProjectionOutcome.DUPLICATE, ProjectionOutcome.STALE],
        )
        self.assertEqual(self.read.metric_results, {calculation.calculation_id: calculation})
        self.assertEqual(len(self.read.processed), 2)

    def test_same_calculation_with_other_data_is_a_conflict(self) -> None:
        calculation = a_calculation(uuid4(), NOW - timedelta(hours=1))
        self.project(self.event(calculation))
        payload = metric_envelope(calculation)["payload"]
        changed = self.event(
            calculation, payload={**payload, "result": {"value": 239, "unit": "W", "scale": 0}}
        )

        with (
            self.assertLogs("sportapp.projector", level="WARNING") as logs,
            self.assertRaises(MetricResultConflict),
        ):
            self.project(changed)

        self.assertEqual(self.read.metric_results[calculation.calculation_id], calculation)
        for record in logs.records:
            record_fields = getattr(record, "fields", {})
            self.assertEqual(set(record_fields), {"calculation_id"}, "sin datos del resultado")

    def test_handler_acks_projected_and_nacks_conflicts_and_other_versions(self) -> None:
        handler = ProjectionMessageHandler(self.project)
        calculation = a_calculation(uuid4(), NOW - timedelta(hours=1))
        payload = metric_envelope(calculation)["payload"]
        ok = Message(metric_envelope(calculation))
        conflict = Message(
            metric_envelope(calculation, payload={**payload, "measured_at": "2026-10-12T10:00:00Z"})
        )
        version_two = Message(metric_envelope(calculation, aggregate_version=2))

        handler(ok)
        with self.assertLogs("sportapp.projector", level="WARNING"):
            handler(conflict)
            handler(version_two)

        self.assertEqual((ok.acked, conflict.acked, version_two.acked), (True, False, False))
        self.assertTrue(conflict.nacked and version_two.nacked)

    def test_third_method_is_projected_without_changes(self) -> None:
        """HA014: el proyector no depende del catálogo."""
        calculation = TrainingMetricCalculation.calculate(
            athlete_id=uuid4(),
            validated=validate_calculation(recovery_body(), EXTENDED_TEST_CATALOG, NOW),
            now=NOW,
        )

        self.assertEqual(self.project(self.event(calculation)), ProjectionOutcome.PROJECTED)
        self.assertEqual(self.read.metric_results[calculation.calculation_id], calculation)
        self.assertEqual(calculation.measured_at.tzinfo, UTC)


if __name__ == "__main__":
    unittest.main()
