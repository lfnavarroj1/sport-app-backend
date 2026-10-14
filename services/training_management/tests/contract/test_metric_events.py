"""HU034: el sobre que publica el servicio cumple `indicador.calculado.v1` (ADR-030) y el
proyector acepta lo que publica el productor."""

import json
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from sportapp_platform.testing.contracts import repository_root
from training_management.application.messages import parse_training_event
from training_management.domain.metrics.catalog import TRAINING_METRICS_CATALOG
from training_management.domain.metrics.events import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    calculated_payload,
)
from training_management.domain.metrics.model import (
    TrainingMetricCalculation,
    validate_calculation,
)
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


def calculation(
    metric: str, method: str, code: str, value: int, unit: str
) -> TrainingMetricCalculation:
    validated = validate_calculation(
        {
            "calculation_id": str(uuid.uuid4()),
            "metric_code": metric,
            "method_code": method,
            "method_version": 1,
            "measured_at": "2026-10-12T15:30:00Z",
            "inputs": [{"code": code, "value": value, "unit": unit}],
        },
        TRAINING_METRICS_CATALOG,
        NOW,
    )
    return TrainingMetricCalculation.calculate(
        athlete_id=uuid.uuid4(), validated=validated, now=NOW + timedelta(seconds=1)
    )


def published(calculated: TrainingMetricCalculation) -> dict[str, Any]:
    envelope = build_envelope(
        ClaimedEvent(
            event_id=uuid.uuid4(),
            event_type=EVENT_TYPE,
            schema_version=SCHEMA_VERSION,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=calculated.calculation_id,
            aggregate_version=calculated.version,
            occurred_at=calculated.calculated_at,
            correlation_id=uuid.uuid4(),
            causation_id=str(uuid.uuid4()),
            payload=calculated_payload(calculated),
            attempts=0,
        )
    )
    # Ida y vuelta por JSON: así viaja por Pub/Sub.
    loaded: dict[str, Any] = json.loads(json.dumps(envelope))
    return loaded


class MetricCalculatedEventContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = Draft202012Validator(
            json.loads(SCHEMA.read_text(encoding="utf-8")), format_checker=FormatChecker()
        )
        self.ftp = calculation("FTP_ESTIMATED", "FTP_20_MIN_95_V1", "mean_power_watts", 250, "W")
        self.vo2max = calculation(
            "VO2MAX_ESTIMATED", "COOPER_12_MIN_V1", "distance_meters", 2400, "m"
        )

    def test_published_envelopes_match_the_schema(self) -> None:
        for calculated, result in (
            (self.ftp, {"value": 238, "unit": "W", "scale": 0}),
            (self.vo2max, {"value": 42.4, "unit": "mL/kg/min", "scale": 1}),
        ):
            body = published(calculated)
            with self.subTest(method=calculated.method_code):
                self.validator.validate(body)
                self.assertEqual(
                    (body["aggregate_id"], body["aggregate_version"], body["producer"]),
                    (str(calculated.calculation_id), 1, "training-management"),
                )
                self.assertEqual(body["payload"]["result"], result)
                self.assertEqual(
                    (body["payload"]["status"], body["payload"]["disclaimer_key"]),
                    ("CONFIRMED", "estimated_not_diagnostic"),
                )
                self.assertEqual(body["payload"]["measured_at"], "2026-10-12T15:30:00Z")
                self.assertTrue(body["payload"]["calculated_at"].endswith("Z"))

    def test_schema_rejects_personal_or_clinical_data(self) -> None:
        for extra in ("athlete_name", "diagnosis", "heart_rate"):
            body = published(self.ftp)
            body["payload"][extra] = "dato no autorizado"
            with self.subTest(field=extra):
                self.assertFalse(self.validator.is_valid(body))
        body = published(self.ftp)
        body["payload"]["result"]["classification"] = "excelente"
        self.assertFalse(self.validator.is_valid(body))
        body = published(self.ftp)
        body["payload"]["inputs"][0]["weight_kg"] = 70
        self.assertFalse(self.validator.is_valid(body))

    def test_schema_rejects_other_versions_and_non_utc_instants(self) -> None:
        for path, value in (
            (("aggregate_version",), 2),
            (("payload", "status"), "PENDING_SYNC"),
            (("payload", "measured_at"), "2026-10-12T10:30:00-05:00"),
            (("payload", "result", "value"), "238"),
        ):
            body = published(self.ftp)
            target = body
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(field=".".join(path)):
                self.assertFalse(self.validator.is_valid(body))

    def test_projector_accepts_what_the_producer_publishes(self) -> None:
        for calculated in (self.ftp, self.vo2max):
            body = published(calculated)
            with self.subTest(method=calculated.method_code):
                event = parse_training_event(body)
                self.assertEqual(event.event_id, uuid.UUID(body["event_id"]))


if __name__ == "__main__":
    unittest.main()
