"""HU042: el sobre que publica el servicio cumple `reporte_salud.registrado.v1` (ADR-037) y el
proyector acepta lo que publica el productor."""

import json
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from sportapp_platform.testing.contracts import repository_root
from training_management.application.messages import (
    HealthReportRegisteredEvent,
    parse_training_event,
)
from training_management.domain.health.events import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    registered_payload,
)
from training_management.domain.health.model import HealthReport, validate_report
from training_management.infrastructure.persistence_write.outbox import ClaimedEvent
from training_management.workers.outbox_publisher import build_envelope

SCHEMA = (
    repository_root(Path(__file__).resolve())
    / "contracts"
    / "events"
    / "entrenamientos"
    / f"{EVENT_TYPE}.schema.json"
)
NOW = datetime(2026, 10, 12, 16, 0, tzinfo=UTC)
CONSENT = "local-dev"


def report(**overrides: Any) -> HealthReport:
    body: dict[str, Any] = {
        "report_id": str(uuid.uuid4()),
        "type": "INJURY",
        "body_area": "ANKLE_FOOT",
        "severity": "HIGH",
        "occurred_at": "2026-10-12T15:30:00Z",
        "consent": {"version": CONSENT},
        **overrides,
    }
    return HealthReport.register(
        athlete_id=uuid.uuid4(),
        data=validate_report(body, NOW, CONSENT),
        now=NOW + timedelta(seconds=1),
    )


def published(registered: HealthReport) -> dict[str, Any]:
    envelope = build_envelope(
        ClaimedEvent(
            event_id=uuid.uuid4(),
            event_type=EVENT_TYPE,
            schema_version=SCHEMA_VERSION,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=registered.report_id,
            aggregate_version=registered.version,
            occurred_at=registered.reported_at,
            correlation_id=uuid.uuid4(),
            causation_id=str(uuid.uuid4()),
            payload=registered_payload(registered),
            attempts=0,
        )
    )
    # Ida y vuelta por JSON: así viaja por Pub/Sub.
    loaded: dict[str, Any] = json.loads(json.dumps(envelope))
    return loaded


class HealthReportRegisteredContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = Draft202012Validator(
            json.loads(SCHEMA.read_text(encoding="utf-8")), format_checker=FormatChecker()
        )

    def test_published_envelopes_match_the_schema(self) -> None:
        for registered in (
            report(),
            report(session_id=str(uuid.uuid4()), expected_recovery_days=14),
            report(type="DISCOMFORT", body_area="OTHER", severity="LOW", expected_recovery_days=0),
        ):
            body = published(registered)
            with self.subTest(report_id=registered.report_id):
                self.validator.validate(body)
                self.assertEqual(
                    (body["aggregate_id"], body["aggregate_version"], body["producer"]),
                    (str(registered.report_id), 1, "training-management"),
                )
                self.assertEqual(body["payload"]["status"], "OPEN")
                self.assertEqual(body["payload"]["occurred_at"], "2026-10-12T15:30:00Z")
                self.assertTrue(body["payload"]["reported_at"].endswith("Z"))

    def test_the_projector_accepts_what_the_producer_publishes(self) -> None:
        registered = report(session_id=str(uuid.uuid4()))

        event = parse_training_event(published(registered))

        assert isinstance(event, HealthReportRegisteredEvent)
        self.assertEqual(event.report, registered)

    def test_schema_rejects_free_text_and_unknown_values(self) -> None:
        base = published(report())
        for name, change in (
            ("texto libre", {"notes": "me duele el tobillo"}),
            ("diagnóstico", {"diagnosis": "esguince"}),
            ("zona fuera de catálogo", {"body_area": "TOE"}),
            ("estado cerrado", {"status": "CLOSED"}),
            ("recuperación fuera de rango", {"expected_recovery_days": 366}),
            ("instante con desfase", {"occurred_at": "2026-10-12T10:30:00-05:00"}),
        ):
            envelope = {**base, "payload": {**base["payload"], **change}}
            with self.subTest(name), self.assertRaises(ValidationError):
                self.validator.validate(envelope)

    def test_schema_matches_the_domain_catalogs(self) -> None:
        payload = json.loads(SCHEMA.read_text(encoding="utf-8"))["properties"]["payload"]
        properties = payload["properties"]

        self.assertEqual(set(payload["required"]), set(registered_payload(report())))
        self.assertEqual(properties["type"]["enum"], ["DISCOMFORT", "INJURY", "INCAPACITY"])
        self.assertEqual(len(properties["body_area"]["enum"]), 14)
        self.assertEqual(properties["severity"]["enum"], ["LOW", "MODERATE", "HIGH"])


if __name__ == "__main__":
    unittest.main()
