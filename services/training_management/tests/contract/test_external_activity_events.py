"""HU031: el consumidor de Entrenamientos acepta exactamente `actividad.importada.v1`.

El contrato con `third_party_management` es solo el esquema versionado
`contracts/events/terceros/actividad.importada.v1.schema.json` (no se importa el productor):
un ejemplo válido para el esquema lo acepta el parser, y lo que el esquema rechaza por forma
(campos de más o de menos, constantes, tipos) también lo rechaza el parser.
"""

import json
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from sportapp_platform.testing.contracts import repository_root
from training_management.application.external_activity import (
    ENVELOPE_FIELDS,
    PAYLOAD_FIELDS,
    parse_external_activity_event,
)
from training_management.application.messages import InvalidEvent

SCHEMA = (
    repository_root(Path(__file__).resolve())
    / "contracts"
    / "events"
    / "terceros"
    / "actividad.importada.v1.schema.json"
)
STARTED = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)


def utc(instant: datetime) -> str:
    return instant.isoformat().replace("+00:00", "Z")


def example() -> dict[str, Any]:
    """Sobre como lo publica `terceros-outbox-publisher` (datos sintéticos)."""
    activity_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "strava:9876543210"))
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "actividad.importada.v1",
        "occurred_at": "2026-10-20T13:05:00Z",
        "producer": "third-party-management",
        "aggregate_type": "external_activity",
        "aggregate_id": activity_id,
        "aggregate_version": 2,
        "correlation_id": None,
        "causation_id": str(uuid.uuid4()),
        "schema_version": 1,
        "payload": {
            "activity_id": activity_id,
            "user_id": str(uuid.uuid4()),
            "provider": "STRAVA",
            "external_activity_id": "9876543210",
            "sport": "cycling",
            "name": "Ruta sintética",
            "started_at": utc(STARTED),
            "ended_at": utc(STARTED + timedelta(seconds=5400)),
            "time_zone": "America/Bogota",
            "duration_seconds": 5400,
            "moving_seconds": 5100,
            "distance_meters": 40500,
            "elevation_gain_meters": None,
            "mapping_version": "strava-activity.v1",
            "version": 2,
        },
    }


class ExternalActivityEventContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        cls.validator = Draft202012Validator(cls.schema, format_checker=FormatChecker())

    def assert_schema_and_parser_reject(self, envelope: dict[str, Any]) -> None:
        self.assertFalse(self.validator.is_valid(envelope))
        with self.assertRaises(InvalidEvent):
            parse_external_activity_event(envelope)

    def test_valid_example_is_accepted_by_schema_and_parser(self) -> None:
        envelope = example()

        self.validator.validate(envelope)
        event = parse_external_activity_event(envelope)

        self.assertEqual(str(event.activity.activity_id), envelope["aggregate_id"])
        self.assertEqual(event.activity.version, 2)
        self.assertEqual(event.activity.distance_meters, 40500)

    def test_parser_fields_are_exactly_the_schema_fields(self) -> None:
        payload_schema = self.schema["properties"]["payload"]

        self.assertEqual(ENVELOPE_FIELDS, set(self.schema["required"]))
        self.assertEqual(ENVELOPE_FIELDS, set(self.schema["properties"]))
        self.assertEqual(PAYLOAD_FIELDS, set(payload_schema["required"]))
        self.assertEqual(PAYLOAD_FIELDS, set(payload_schema["properties"]))
        self.assertFalse(self.schema["additionalProperties"])
        self.assertFalse(payload_schema["additionalProperties"])

    def test_extra_or_missing_fields_are_rejected(self) -> None:
        envelope = example()
        payload = envelope["payload"]
        cases = {
            "envelope_extra": {**envelope, "ordering_key": payload["user_id"]},
            "payload_extra": {**envelope, "payload": {**payload, "raw": {"id": 1}}},
            "payload_token": {**envelope, "payload": {**payload, "access_token": "x"}},
            "envelope_missing": {k: v for k, v in envelope.items() if k != "causation_id"},
            "payload_missing": {
                **envelope,
                "payload": {k: v for k, v in payload.items() if k != "time_zone"},
            },
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                self.assert_schema_and_parser_reject(body)

    def test_constants_and_types_are_rejected_by_both(self) -> None:
        envelope = example()
        payload = envelope["payload"]
        cases = {
            "schema_version": {**envelope, "schema_version": 2},
            "producer": {**envelope, "producer": "training-management"},
            "aggregate_type": {**envelope, "aggregate_type": "training_session"},
            "mapping_version": {
                **envelope,
                "payload": {**payload, "mapping_version": "strava-activity.v2"},
            },
            "provider": {**envelope, "payload": {**payload, "provider": "GARMIN"}},
            "sport": {**envelope, "payload": {**payload, "sport": "golf"}},
            "duration": {**envelope, "payload": {**payload, "duration_seconds": 0}},
            "distance": {**envelope, "payload": {**payload, "distance_meters": 10.5}},
            "name_length": {**envelope, "payload": {**payload, "name": "x" * 121}},
            "external_id": {**envelope, "payload": {**payload, "external_activity_id": 9}},
            "started_not_utc": {
                **envelope,
                "payload": {**payload, "started_at": "2026-10-20T06:00:00-05:00"},
            },
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                self.assert_schema_and_parser_reject(body)


if __name__ == "__main__":
    unittest.main()
