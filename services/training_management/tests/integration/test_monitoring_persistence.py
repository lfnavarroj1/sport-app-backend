"""HU036 contra PostgreSQL real: 3.600 muestras de una sesión de 60 min con la evaluación en la
misma transacción, lotes duplicados y concurrentes, `RISK_STOP` idempotente, umbral, retención
de 90 días y proyección reconstruible de resúmenes y alertas."""

import threading
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from support import (
    TrainingDatabases,
    athlete_and_professional,
    start_command,
    training_databases,
)

from training_management.application.commands.devices import (
    RegisterDevice,
    RegisterDeviceCommand,
)
from training_management.application.commands.measurements import (
    IngestMeasurements,
    IngestMeasurementsCommand,
)
from training_management.application.commands.monitoring_settings import (
    UpdateMonitoringSettings,
)
from training_management.application.commands.training_sessions import (
    FinishTrainingSessionCommand,
)
from training_management.application.errors import TrainingSessionAlreadyFinished
from training_management.application.messages import parse_training_event
from training_management.application.monitoring_threshold import ResolveThreshold
from training_management.domain.devices.model import DeviceInput
from training_management.domain.monitoring.events import MEASUREMENT_EVENT_TYPE, RISK_EVENT_TYPE
from training_management.domain.sessions.model import SessionEndReason
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.persistence_read.models import (
    SessionMonitoringViewRow,
    SessionRiskAlertViewRow,
)
from training_management.infrastructure.persistence_write.models import (
    MonitoringSettingsRow,
    OutboxEventRow,
    SessionMonitoringStateRow,
    TrainingMeasurementRow,
    TrainingRiskAlertRow,
    TrainingSessionRow,
)
from training_management.infrastructure.persistence_write.outbox import ClaimedEvent
from training_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyDeviceUnitOfWork,
    SqlAlchemyMeasurementRetention,
    SqlAlchemyMeasurementUnitOfWork,
    SqlAlchemyMonitoringSettingsUnitOfWork,
)
from training_management.workers.outbox_publisher import build_envelope

DEVICE_ID = "d" * 64


class AlwaysCapable:
    def has_capability(self, user_id: Any, capability: str) -> bool:
        return True


class NoBirthDate:
    def birth_date(self, athlete_id: Any) -> None:
        return None


class ManualClock:
    """Reloj del backend que avanza con la sesión simulada."""

    def __init__(self, now: datetime) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


def z(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def batch(start: datetime, values: list[int]) -> list[dict[str, Any]]:
    return [
        {
            "measured_at": z(start + timedelta(seconds=offset)),
            "type": "HEART_RATE",
            "value": value,
            "unit": "bpm",
        }
        for offset, value in enumerate(values)
    ]


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


class MonitoringPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: TrainingDatabases = self.enterContext(training_databases("hu036"))
        self.athlete, _ = athlete_and_professional()
        self.started_at = (datetime.now(UTC) - timedelta(minutes=70)).replace(microsecond=0)
        self.clock = ManualClock(self.started_at + timedelta(seconds=5))
        RegisterDevice(
            unit_of_work=lambda: SqlAlchemyDeviceUnitOfWork(self.db.writes),
            capabilities=AlwaysCapable(),
            clock=SystemClock(),
        )(
            self.athlete,
            RegisterDeviceCommand(
                device=DeviceInput(DEVICE_ID, "Banda", "HEART_RATE_MONITOR", "BLE_HRS"),
                idempotency_key=uuid4(),
            ),
        )
        self.session = self.db.start_session()(
            self.athlete, start_command(started_at=z(self.started_at))
        ).session
        self.settings = lambda: SqlAlchemyMonitoringSettingsUnitOfWork(self.db.writes)
        self.ingest = IngestMeasurements(
            unit_of_work=lambda: SqlAlchemyMeasurementUnitOfWork(self.db.writes),
            capabilities=AlwaysCapable(),
            thresholds=ResolveThreshold(
                unit_of_work=self.settings, birth_dates=NoBirthDate(), clock=self.clock
            ),
            clock=self.clock,
        )
        self.db.discard_pending_outbox()

    def send(self, samples: list[dict[str, Any]]) -> Any:
        return self.ingest(
            self.athlete,
            IngestMeasurementsCommand(
                session_id=str(self.session.session_id),
                body={"device_id": DEVICE_ID, "samples": samples},
            ),
        )

    def finish_command(
        self, ended_at: str, reason: str | None = None
    ) -> FinishTrainingSessionCommand:
        return FinishTrainingSessionCommand(
            session_id=str(self.session.session_id),
            ended_at=ended_at,
            idempotency_key=uuid4(),
            reason=reason,
        )

    def outbox(self, event_type: str) -> list[OutboxEventRow]:
        with self.db.writes() as session:
            return list(
                session.scalars(
                    select(OutboxEventRow)
                    .where(OutboxEventRow.event_type == event_type)
                    .order_by(OutboxEventRow.aggregate_version)
                )
            )

    def project_outbox(self) -> None:
        project = self.db.project()
        for row in [*self.outbox(MEASUREMENT_EVENT_TYPE), *self.outbox(RISK_EVENT_TYPE)]:
            envelope = build_envelope(
                ClaimedEvent(
                    event_id=row.event_id,
                    event_type=row.event_type,
                    schema_version=row.schema_version,
                    aggregate_type=row.aggregate_type,
                    aggregate_id=row.aggregate_id,
                    aggregate_version=row.aggregate_version,
                    occurred_at=row.occurred_at,
                    correlation_id=row.correlation_id,
                    causation_id=row.causation_id,
                    payload=row.payload,
                    attempts=0,
                )
            )
            project(parse_training_event(envelope))

    def test_sixty_minutes_of_samples_with_a_risk_episode(self) -> None:
        """CA1-CA2: 3.600 muestras en 60 lotes; episodio de 3 min sobre 171 → 3 alertas."""
        statuses = []
        for minute in range(60):
            start = self.started_at + timedelta(seconds=60 * minute)
            value = 180 if 10 <= minute < 13 else 120
            self.clock.current = start + timedelta(seconds=60)
            result = self.send(batch(start, [value] * 60))
            self.assertEqual((result.accepted, result.duplicates, result.rejected), (60, 0, ()))
            statuses.append(result.evaluation.status.value)
        self.assertEqual(self.db.count(TrainingMeasurementRow), 3600)
        self.assertEqual(statuses[:10], ["OK"] * 10)
        self.assertEqual(set(statuses[10:13]), {"RISK"})
        self.assertEqual(statuses[13:], ["OK"] * 47)
        episode = self.started_at + timedelta(minutes=10, seconds=14)
        with self.db.writes() as session:
            alerts = list(
                session.scalars(
                    select(TrainingRiskAlertRow.detected_at).order_by(
                        TrainingRiskAlertRow.detected_at
                    )
                )
            )
            state = session.get(SessionMonitoringStateRow, self.session.session_id)
        self.assertEqual(alerts, [episode + timedelta(seconds=60 * k) for k in range(3)])
        assert state is not None
        self.assertEqual((state.summary_version, state.alert_count), (60, 3))
        summaries = self.outbox(MEASUREMENT_EVENT_TYPE)
        self.assertEqual([row.aggregate_version for row in summaries], list(range(1, 61)))
        self.assertEqual([row.aggregate_version for row in self.outbox(RISK_EVENT_TYPE)], [1, 2, 3])

        self.project_outbox()
        self.project_outbox()  # Reentrega completa: sin efectos.
        with self.db.reads() as session:
            view = session.get(SessionMonitoringViewRow, self.session.session_id)
            alert_rows = session.scalars(select(SessionRiskAlertViewRow)).all()
        assert view is not None
        self.assertEqual(
            (view.samples_count, view.max_value, view.last_value, view.version),
            (3600, 180, 120, 60),
        )
        self.assertEqual(str(view.avg_value), "123.0")
        self.assertEqual(len(alert_rows), 3)

    def test_concurrent_duplicate_batches_store_and_alert_once(self) -> None:
        start = self.started_at
        self.clock.current = start + timedelta(seconds=15)
        risky = batch(start, [190] * 15)
        results, errors = concurrently(5, lambda: self.send(risky))
        self.assertEqual(errors, [])
        self.assertEqual(sum(result.accepted for result in results), 15)
        self.assertEqual(sum(result.duplicates for result in results), 60)
        self.assertEqual(sum(len(result.evaluation.alerts) for result in results), 1)
        self.assertEqual({result.evaluation.status.value for result in results}, {"RISK"})
        self.assertEqual(len(self.outbox(MEASUREMENT_EVENT_TYPE)), 1)
        self.assertEqual(self.db.count(TrainingRiskAlertRow), 1)

    def test_concurrent_distinct_batches_get_consecutive_summary_versions(self) -> None:
        self.clock.current = self.started_at + timedelta(seconds=30)
        batches = [batch(self.started_at + timedelta(seconds=5 * k), [130] * 5) for k in range(5)]
        pending = list(batches)
        lock = threading.Lock()

        def next_batch() -> Any:
            with lock:
                samples = pending.pop()
            return self.send(samples)

        _, errors = concurrently(5, next_batch)
        self.assertEqual(errors, [])
        versions = [row.aggregate_version for row in self.outbox(MEASUREMENT_EVENT_TYPE)]
        self.assertEqual(versions, [1, 2, 3, 4, 5])
        self.assertEqual(self.db.count(TrainingMeasurementRow), 25)

    def test_risk_stop_finishes_once_and_keeps_the_measurements(self) -> None:
        """CA4: transición única con `RISK_STOP`; las mediciones se conservan."""
        self.clock.current = self.started_at + timedelta(seconds=15)
        self.send(batch(self.started_at, [190] * 15))
        finish = self.db.finish_session()
        ended_at = z(datetime.now(UTC).replace(microsecond=0))
        first = finish(self.athlete, self.finish_command(ended_at, "RISK_STOP"))
        again = finish(self.athlete, self.finish_command(ended_at, "RISK_STOP"))
        self.assertIs(first.session.end_reason, SessionEndReason.RISK_STOP)
        self.assertTrue(again.replayed)
        with self.assertRaises(TrainingSessionAlreadyFinished):
            finish(self.athlete, self.finish_command(ended_at))
        with self.db.writes() as session:
            row = session.get(TrainingSessionRow, self.session.session_id)
        assert row is not None
        self.assertEqual((row.status, row.end_reason, row.version), ("FINISHED", "RISK_STOP", 2))
        self.assertEqual(self.db.count(TrainingMeasurementRow), 15)
        with self.assertRaises(TrainingSessionAlreadyFinished):
            self.send(batch(self.started_at + timedelta(seconds=15), [120]))

    def test_own_threshold_is_used_and_updates_are_serialized(self) -> None:
        update = UpdateMonitoringSettings(
            unit_of_work=self.settings, capabilities=AlwaysCapable(), clock=SystemClock()
        )
        values = iter([150, 150, 160, 160, 170])
        lock = threading.Lock()

        def put() -> Any:
            with lock:
                value = next(values)
            return update(self.athlete, {"max_heart_rate_alert_bpm": value})

        _, errors = concurrently(5, put)
        self.assertEqual(errors, [])
        self.assertEqual(self.db.count(MonitoringSettingsRow), 1)
        update(self.athlete, {"max_heart_rate_alert_bpm": 140})
        self.clock.current = self.started_at + timedelta(seconds=15)
        result = self.send(batch(self.started_at, [145] * 15))
        self.assertEqual(result.evaluation.status.value, "RISK")
        with self.db.writes() as session:
            alert = session.scalar(select(TrainingRiskAlertRow))
        assert alert is not None
        self.assertEqual(alert.threshold_bpm, 140)

    def test_retention_purges_only_measurements_older_than_ninety_days(self) -> None:
        self.clock.current = self.started_at + timedelta(seconds=5)
        self.send(batch(self.started_at, [120] * 5))
        old = datetime.now(UTC) - timedelta(days=91)
        with self.db.writes() as session, session.begin():
            for second in range(5):
                session.execute(
                    text(
                        "INSERT INTO training_measurement VALUES "
                        "(:session, :device, :at, 'HEART_RATE', 120, 'bpm')"
                    ),
                    {
                        "session": self.session.session_id,
                        "device": DEVICE_ID,
                        "at": old + timedelta(seconds=second),
                    },
                )
        store = SqlAlchemyMeasurementRetention(self.db.writes)
        cutoff = datetime.now(UTC) - timedelta(days=90)
        self.assertEqual(store.purge_measurements(cutoff, 2), 2)
        self.assertEqual(store.purge_measurements(cutoff, 10), 3)
        self.assertEqual(store.purge_measurements(cutoff, 10), 0)
        self.assertEqual(self.db.count(TrainingMeasurementRow), 5)

    def test_database_rejects_inconsistent_monitoring_rows(self) -> None:
        statements = [
            "UPDATE training_session SET end_reason = 'RISK_STOP'",
            "UPDATE training_session SET end_reason = 'TIRED'",
            "INSERT INTO monitoring_settings VALUES (gen_random_uuid(), 99, 1, now(), now())",
        ]
        for statement in statements:
            with self.subTest(statement=statement), self.assertRaises(IntegrityError):
                with self.db.writes() as session, session.begin():
                    session.execute(text(statement))


if __name__ == "__main__":
    unittest.main()
