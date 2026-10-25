"""HU042 CA4: `reporte_salud.registrado.v1` se proyecta una vez en `health_report_view`."""

import dataclasses
import unittest
from typing import Any
from uuid import uuid4

from fakes import FixedClock, InMemoryProjectionUnitOfWork, InMemoryReadStore
from health_fakes import a_report, health_envelope
from test_workers import Message

from training_management.application.messages import (
    HealthReportRegisteredEvent,
    InvalidEvent,
    parse_training_event,
)
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.application.projections.snapshot_projection import TrainingProjector
from training_management.domain.health.model import HealthReport
from training_management.workers.read_projector import ProjectionMessageHandler


class HealthReportEventParsingTest(unittest.TestCase):
    def test_published_envelope_is_parsed_with_the_full_state(self) -> None:
        report = a_report(session_id=str(uuid4()))
        envelope = health_envelope(report)

        event = parse_training_event(envelope)

        assert isinstance(event, HealthReportRegisteredEvent)
        self.assertEqual(event.report, report)
        self.assertEqual(event.aggregate_id, report.report_id)
        self.assertEqual(str(event.event_id), envelope["event_id"])

    def test_invalid_envelopes_are_rejected(self) -> None:
        report = a_report()
        payload = health_envelope(report)["payload"]
        cases: dict[str, dict[str, Any]] = {
            "aggregate_id_mismatch": health_envelope(report, aggregate_id=str(uuid4())),
            "unsupported_schema_version": health_envelope(report, schema_version=2),
            "aggregate_version_invalid": health_envelope(report, aggregate_version=0),
            "body_area_invalid": health_envelope(report, payload={**payload, "body_area": "TOE"}),
            "status_invalid": health_envelope(report, payload={**payload, "status": "CLOSED"}),
            "payload_not_object": health_envelope(report, payload=[]),
        }
        for reason, envelope in cases.items():
            with self.subTest(reason=reason), self.assertRaises(InvalidEvent) as caught:
                parse_training_event(envelope)
            self.assertEqual(caught.exception.reason, reason)


class HealthReportProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.read = InMemoryReadStore()
        self.project = TrainingProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.read), clock=FixedClock()
        )

    def event(self, report: HealthReport, **overrides: Any) -> HealthReportRegisteredEvent:
        parsed = parse_training_event(health_envelope(report, **overrides))
        assert isinstance(parsed, HealthReportRegisteredEvent)
        return parsed

    def test_projects_once_despite_repeated_deliveries(self) -> None:
        """CA4: Pub/Sub repite la entrega y la vista incorpora el reporte una sola vez."""
        report = a_report()
        event = self.event(report)

        outcomes = [self.project(event), self.project(event), self.project(self.event(report))]

        self.assertEqual(
            outcomes,
            [ProjectionOutcome.PROJECTED, ProjectionOutcome.DUPLICATE, ProjectionOutcome.STALE],
        )
        self.assertEqual(self.read.health_reports, {report.report_id: report})
        self.assertEqual(len(self.read.processed), 2)

    def test_rebuild_from_empty_view_reproduces_the_same_state(self) -> None:
        reports = [a_report(), a_report(severity="HIGH"), a_report(type="INJURY")]
        for report in reports:
            self.project(self.event(report))
        projected = dict(self.read.health_reports)

        self.read = InMemoryReadStore()
        self.project = TrainingProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.read), clock=FixedClock()
        )
        for report in reversed(reports):
            self.project(self.event(report))

        self.assertEqual(self.read.health_reports, projected)

    def test_a_greater_version_replaces_and_a_lower_one_never_goes_back(self) -> None:
        report = a_report()
        later = dataclasses.replace(report, version=2)
        self.read.health_reports[report.report_id] = later

        outcome = self.project(self.event(report))

        self.assertEqual(outcome, ProjectionOutcome.STALE)
        self.assertEqual(self.read.health_reports[report.report_id], later)

    def test_handler_acks_projected_and_nacks_invalid(self) -> None:
        handler = ProjectionMessageHandler(self.project)
        report = a_report()
        ok = Message(health_envelope(report))
        repeated = Message(health_envelope(report))
        invalid = Message(health_envelope(report, aggregate_id=str(uuid4())))

        with self.assertLogs("sportapp.projector", level="INFO") as logs:
            handler(ok)
            handler(repeated)
            handler(invalid)

        self.assertTrue(ok.acked and repeated.acked)
        self.assertTrue(invalid.nacked and not invalid.acked)
        processed = [r for r in logs.records if r.getMessage() == "projector.processed"]
        self.assertEqual(processed[0].__dict__["fields"]["aggregate_id"], str(report.report_id))
        for record in logs.records:
            text = f"{record.getMessage()} {record.__dict__.get('fields', '')}"
            for value in ("DISCOMFORT", "KNEE", "MODERATE"):
                self.assertNotIn(value, text, "el proyector no registra datos de salud")


if __name__ == "__main__":
    unittest.main()
