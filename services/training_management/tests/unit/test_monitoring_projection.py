"""HU036 (CMP-003): proyección de `medicion.recibida.v1` en `session_monitoring_view` (solo la
versión siguiente, duplicados, huecos hacia la DLQ, reconstrucción) y de `riesgo.detectado.v1`
en `session_risk_alert_view` (inserción única)."""

import json
import unittest
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fakes import NOW, FixedClock, InMemoryProjectionUnitOfWork, InMemoryReadStore

from training_management.application.messages import MeasurementReceivedEvent, RiskDetectedEvent
from training_management.application.ports import RiskAlertView
from training_management.application.projections.monitoring_projection import (
    MonitoringVersionGap,
    accumulate,
)
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.application.projections.snapshot_projection import TrainingProjector
from training_management.domain.monitoring.events import measurement_payload
from training_management.domain.monitoring.model import BatchSummary
from training_management.workers.read_projector import ProjectionMessageHandler

SESSION = uuid4()
DEVICE = "c" * 64


def summary(version: int, last: int, maximum: int, average: str, *, count: int, end: int) -> Any:
    return MeasurementReceivedEvent(
        event_id=uuid4(),
        correlation_id=None,
        summary=BatchSummary(
            session_id=SESSION,
            device_id=DEVICE,
            window_from=NOW + timedelta(seconds=end - count + 1),
            window_to=NOW + timedelta(seconds=end),
            samples_accepted=count,
            last_value=last,
            max_value=maximum,
            avg_value=Decimal(average),
            version=version,
        ),
    )


def risk(sequence: int) -> RiskDetectedEvent:
    return RiskDetectedEvent(
        event_id=uuid4(),
        correlation_id=None,
        alert=RiskAlertView(
            alert_id=uuid4(),
            session_id=SESSION,
            device_id=DEVICE,
            rule="HR_ABOVE_THRESHOLD_V1",
            severity="HIGH",
            detected_at=NOW + timedelta(seconds=sequence * 60),
            sequence=sequence,
        ),
    )


class MonitoringProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryReadStore()
        self.project = TrainingProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.store), clock=FixedClock()
        )

    def test_summaries_accumulate_in_version_order(self) -> None:
        first = summary(1, 120, 130, "125.0", count=5, end=4)
        second = summary(2, 140, 150, "145.5", count=5, end=9)
        self.assertIs(self.project(first), ProjectionOutcome.PROJECTED)
        self.assertIs(self.project(second), ProjectionOutcome.PROJECTED)
        view = self.store.monitoring[SESSION]
        self.assertEqual(
            (
                view.last_measured_at,
                view.last_value,
                view.samples_count,
                view.max_value,
                view.avg_value,
                view.version,
            ),
            (NOW + timedelta(seconds=9), 140, 10, 150, Decimal("135.3"), 2),
        )

    def test_duplicates_and_older_versions_change_nothing(self) -> None:
        first = summary(1, 120, 130, "125.0", count=5, end=4)
        self.project(first)
        self.assertIs(self.project(first), ProjectionOutcome.DUPLICATE)
        again = summary(1, 120, 130, "125.0", count=5, end=4)
        self.assertIs(self.project(again), ProjectionOutcome.STALE)
        self.assertEqual(self.store.monitoring[SESSION].samples_count, 5)

    def test_a_missing_version_fails_without_effects(self) -> None:
        self.project(summary(1, 120, 130, "125.0", count=5, end=4))
        gap = summary(3, 140, 150, "145.0", count=5, end=14)
        with self.assertRaises(MonitoringVersionGap):
            self.project(gap)
        self.assertEqual(self.store.monitoring[SESSION].version, 1)
        self.assertNotIn(gap.event_id, self.store.processed)

    def test_gap_is_not_acknowledged_so_it_is_retried_and_ends_in_dlq(self) -> None:
        message = _Message(_envelope(summary(2, 140, 150, "145.0", count=5, end=9)))
        ProjectionMessageHandler(self.project)(message)
        self.assertEqual((message.acked, message.nacked), (False, True))

    def test_out_of_order_samples_keep_the_most_recent_value(self) -> None:
        self.project(summary(1, 150, 150, "150.0", count=5, end=20))
        self.project(summary(2, 110, 110, "110.0", count=5, end=4))
        view = self.store.monitoring[SESSION]
        self.assertEqual(
            (view.last_measured_at, view.last_value, view.avg_value),
            (NOW + timedelta(seconds=20), 150, Decimal("130.0")),
        )

    def test_rebuild_from_the_same_events_gives_the_same_view(self) -> None:
        events = [
            summary(1, 120, 130, "125.0", count=5, end=4),
            summary(2, 141, 150, "143.3", count=3, end=7),
            summary(3, 99, 101, "100.1", count=7, end=14),
        ]
        for event in events:
            self.project(event)
        rebuilt = InMemoryReadStore()
        replay = TrainingProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(rebuilt), clock=FixedClock()
        )
        for event in events:
            replay(event)
        self.assertEqual(rebuilt.monitoring, self.store.monitoring)
        manual = None
        for event in events:
            manual = accumulate(manual, event.summary)
        self.assertEqual(manual, self.store.monitoring[SESSION])

    def test_risk_alerts_are_inserted_once(self) -> None:
        alert = risk(1)
        self.assertIs(self.project(alert), ProjectionOutcome.PROJECTED)
        self.assertIs(self.project(alert), ProjectionOutcome.DUPLICATE)
        replayed = RiskDetectedEvent(event_id=uuid4(), correlation_id=None, alert=alert.alert)
        self.assertIs(self.project(replayed), ProjectionOutcome.STALE)
        # Una alerta posterior se proyecta aunque la anterior llegue después.
        self.assertIs(self.project(risk(3)), ProjectionOutcome.PROJECTED)
        self.assertEqual(len(self.store.risk_alerts), 2)


class _Message:
    def __init__(self, envelope: dict[str, Any]) -> None:
        self.data = json.dumps(envelope).encode()
        self.message_id = "m-1"
        self.delivery_attempt = 1
        self.acked = self.nacked = False

    def ack(self) -> None:
        self.acked = True

    def nack(self) -> None:
        self.nacked = True


def _envelope(event: MeasurementReceivedEvent) -> dict[str, Any]:
    return {
        "event_id": str(event.event_id),
        "event_type": "medicion.recibida.v1",
        "occurred_at": "2026-10-12T12:00:00Z",
        "producer": "training-management",
        "aggregate_type": "training_session",
        "aggregate_id": str(SESSION),
        "aggregate_version": event.summary.version,
        "correlation_id": None,
        "causation_id": None,
        "schema_version": 1,
        "payload": measurement_payload(event.summary),
    }


if __name__ == "__main__":
    unittest.main()
