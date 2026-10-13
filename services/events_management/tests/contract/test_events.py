"""El sobre que publica `eventos-outbox-publisher` cumple el esquema versionado de
`evento.creado.v1` (ADR-028), y el proyector acepta exactamente ese contrato."""

import json
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from events_management.api.payload import parse_event
from events_management.application.messages import parse_sport_event_message
from events_management.domain.sport_events.created import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    event_document,
)
from events_management.domain.sport_events.model import SportEvent, validate_event
from events_management.infrastructure.persistence_write.outbox import ClaimedEvent
from events_management.workers.outbox_publisher import build_envelope
from sportapp_platform.testing.contracts import repository_root

SCHEMA_PATH = (
    repository_root(Path(__file__).resolve())
    / "contracts"
    / "events"
    / "eventos"
    / "evento.creado.v1.schema.json"
)
NOW = datetime(2026, 10, 12, 21, 0, tzinfo=UTC)
ADR_EXAMPLE = {
    "name": "Carrera 10K Bogotá",
    "description": "Carrera recreativa de diez kilómetros para corredores aficionados.",
    "discipline": "Running",
    "location": {
        "name": "Parque Metropolitano Simón Bolívar",
        "address": "Avenida Calle 63 # 60-50",
        "city": "Bogotá",
        "country_code": "CO",
    },
    "starts_at": "2026-11-15T07:00:00-05:00",
    "time_zone": "America/Bogota",
    "duration_minutes": 180,
    "capacity": 5000,
}


def published_envelope() -> dict[str, object]:
    event = SportEvent.draft(
        event_id=uuid.uuid4(),
        organizer_id=uuid.uuid4(),
        details=validate_event(parse_event(ADR_EXAMPLE), NOW),
        now=NOW,
    )
    return build_envelope(
        ClaimedEvent(
            event_id=uuid.uuid4(),
            event_type=EVENT_TYPE,
            schema_version=SCHEMA_VERSION,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=event.event_id,
            aggregate_version=event.version,
            occurred_at=NOW,
            correlation_id=uuid.uuid4(),
            causation_id=str(uuid.uuid4()),
            payload=event_document(event),
            attempts=0,
        )
    )


class EventCreatedContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = Draft202012Validator(
            json.loads(SCHEMA_PATH.read_text(encoding="utf-8")), format_checker=FormatChecker()
        )

    def test_published_envelope_matches_schema(self) -> None:
        body = json.loads(json.dumps(published_envelope()))

        self.validator.validate(body)
        payload = body["payload"]
        self.assertEqual(payload["event_id"], body["aggregate_id"])
        self.assertEqual(
            (payload["starts_at"], payload["ends_at"]),
            ("2026-11-15T12:00:00Z", "2026-11-15T15:00:00Z"),
        )

    def test_schema_rejects_payment_or_unknown_data(self) -> None:
        for extra in ({"price": "10000"}, {"organizer_email": "ana@example.com"}):
            body = json.loads(json.dumps(published_envelope()))
            body["payload"].update(extra)
            with self.subTest(extra=extra):
                self.assertFalse(self.validator.is_valid(body))

    def test_schema_requires_utc_instants(self) -> None:
        body = json.loads(json.dumps(published_envelope()))
        body["payload"]["starts_at"] = "2026-11-15T07:00:00-05:00"

        self.assertFalse(self.validator.is_valid(body))

    def test_projector_parses_what_the_producer_publishes(self) -> None:
        body = json.loads(json.dumps(published_envelope()))

        message = parse_sport_event_message(body)

        self.assertEqual(str(message.event.event_id), body["aggregate_id"])
        self.assertEqual(str(message.message_id), body["event_id"])


if __name__ == "__main__":
    unittest.main()
