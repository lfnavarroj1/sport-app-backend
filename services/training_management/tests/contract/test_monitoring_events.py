"""HU036: los sobres que publica el servicio cumplen `medicion.recibida.v1` y
`riesgo.detectado.v1` (ADR-044) y el proyector acepta lo que publica el productor."""

import json
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from sportapp_platform.testing.contracts import repository_root
from training_management.application.messages import (
    InvalidEvent,
    MeasurementReceivedEvent,
    RiskDetectedEvent,
    parse_training_event,
)
from training_management.domain.monitoring.events import (
    AGGREGATE_TYPE,
    MEASUREMENT_EVENT_TYPE,
    RISK_EVENT_TYPE,
    SCHEMA_VERSION,
    measurement_payload,
    risk_payload,
)
from training_management.domain.monitoring.model import evaluate, summarize
from training_management.infrastructure.persistence_write.outbox import ClaimedEvent
from training_management.workers.outbox_publisher import build_envelope

SCHEMAS = repository_root(Path(__file__).resolve()) / "contracts" / "events" / "entrenamientos"
NOW = datetime(2026, 10, 24, 15, 30, tzinfo=UTC)
SESSION = uuid.uuid4()
DEVICE = "b" * 64


def validator(event_type: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / f"{event_type}.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def envelope(
    event_type: str, version: int, payload: dict[str, Any], causation_id: str | None
) -> dict[str, Any]:
    body = build_envelope(
        ClaimedEvent(
            event_id=uuid.uuid4(),
            event_type=event_type,
            schema_version=SCHEMA_VERSION,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=SESSION,
            aggregate_version=version,
            occurred_at=NOW,
            correlation_id=uuid.uuid4(),
            causation_id=causation_id,
            payload=payload,
            attempts=0,
        )
    )
    loaded: dict[str, Any] = json.loads(json.dumps(body))
    return loaded


class MonitoringEventsContractTest(unittest.TestCase):
    def setUp(self) -> None:
        samples = [(NOW - timedelta(seconds=14 - offset), 160 + offset % 3) for offset in range(15)]
        self.summary = summarize(SESSION, DEVICE, samples, 4)
        evaluation = evaluate(
            session_id=SESSION,
            device_id=DEVICE,
            samples=samples,
            new_instants=[instant for instant, _ in samples],
            threshold=150,
            previous_alerts=[],
            now=NOW,
        )
        (self.alert,) = evaluation.alerts
        self.measurement = envelope(
            MEASUREMENT_EVENT_TYPE, 4, measurement_payload(self.summary), None
        )
        self.risk = envelope(RISK_EVENT_TYPE, 2, risk_payload(self.alert), str(uuid.uuid4()))

    def test_envelopes_match_their_schemas(self) -> None:
        validator(MEASUREMENT_EVENT_TYPE).validate(self.measurement)
        validator(RISK_EVENT_TYPE).validate(self.risk)
        self.assertEqual(
            (self.measurement["aggregate_id"], self.measurement["aggregate_version"]),
            (str(SESSION), self.measurement["payload"]["version"]),
        )
        self.assertEqual(self.measurement["payload"]["avg_value"], "161.0")
        self.assertEqual(self.risk["payload"]["detected_at"], "2026-10-24T15:30:00Z")

    def test_schemas_reject_individual_samples_and_health_values_in_the_alert(self) -> None:
        for field, value in (
            ("samples", [{"value": 160}]),
            ("avg_value", 161.0),
            ("avg_value", "161.05"),
            ("device_id", "AA:BB:CC:DD:EE:FF"),
            ("samples_accepted", 61),
            ("last_value", 251),
        ):
            body = json.loads(json.dumps(self.measurement))
            body["payload"][field] = value
            with self.subTest(field=field):
                self.assertFalse(validator(MEASUREMENT_EVENT_TYPE).is_valid(body))
        for field, value in (
            ("value", 180),
            ("max_heart_rate_alert_bpm", 150),
            ("message", "texto"),
            ("rule", "OTHER"),
            ("severity", "LOW"),
        ):
            body = json.loads(json.dumps(self.risk))
            body["payload"][field] = value
            with self.subTest(field=field):
                self.assertFalse(validator(RISK_EVENT_TYPE).is_valid(body))

    def test_projector_accepts_what_the_producer_publishes(self) -> None:
        measurement = parse_training_event(self.measurement)
        assert isinstance(measurement, MeasurementReceivedEvent)
        self.assertEqual(measurement.summary, self.summary)
        risk = parse_training_event(self.risk)
        assert isinstance(risk, RiskDetectedEvent)
        self.assertEqual(
            (risk.alert.alert_id, risk.alert.detected_at, risk.alert.sequence),
            (self.alert.alert_id, NOW, 2),
        )

    def test_projector_rejects_inconsistent_envelopes(self) -> None:
        cases = [
            ("aggregate_version", 5, self.measurement),
            ("aggregate_id", str(uuid.uuid4()), self.measurement),
            ("aggregate_type", "route", self.risk),
            ("schema_version", 2, self.risk),
        ]
        for field, value, original in cases:
            body = json.loads(json.dumps(original))
            body[field] = value
            with self.subTest(field=field), self.assertRaises(InvalidEvent):
                parse_training_event(body)
        body = json.loads(json.dumps(self.measurement))
        body["payload"]["last_value"] = 200
        with self.assertRaises(InvalidEvent):
            parse_training_event(body)


if __name__ == "__main__":
    unittest.main()
