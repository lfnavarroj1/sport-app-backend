"""El sobre que publica `eventos-outbox-publisher` al publicar un evento cumple el esquema
versionado de `evento.publicado.v1` (ADR-033), y el proyector acepta exactamente ese
contrato."""

import json
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from test_events import ADR_EXAMPLE, NOW

from events_management.api.payload import parse_event
from events_management.application.messages import (
    EventPublishedMessage,
    InvalidEvent,
    parse_sport_event_message,
)
from events_management.domain.sport_events.model import SportEvent, validate_event
from events_management.domain.sport_events.published import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    aggregate_document,
)
from events_management.infrastructure.persistence_write.outbox import ClaimedEvent
from events_management.workers.outbox_publisher import build_envelope
from sportapp_platform.testing.contracts import repository_root

SCHEMA_PATH = (
    repository_root(Path(__file__).resolve())
    / "contracts"
    / "events"
    / "eventos"
    / "evento.publicado.v1.schema.json"
)


def publication_envelope() -> dict[str, object]:
    draft = SportEvent.draft(
        event_id=uuid.uuid4(),
        organizer_id=uuid.uuid4(),
        details=validate_event(parse_event(ADR_EXAMPLE), NOW),
        now=NOW,
    )
    event = draft.publish(NOW + timedelta(hours=2, minutes=10))
    return build_envelope(
        ClaimedEvent(
            event_id=uuid.uuid4(),
            event_type=EVENT_TYPE,
            schema_version=SCHEMA_VERSION,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=event.event_id,
            aggregate_version=event.version,
            occurred_at=event.updated_at,
            correlation_id=uuid.uuid4(),
            causation_id=str(uuid.uuid4()),
            payload=aggregate_document(event),
            attempts=0,
        )
    )


class EventPublishedContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = Draft202012Validator(
            json.loads(SCHEMA_PATH.read_text(encoding="utf-8")), format_checker=FormatChecker()
        )

    def test_envelope_matches_schema_with_full_state(self) -> None:
        body = json.loads(json.dumps(publication_envelope()))

        self.validator.validate(body)
        payload = body["payload"]
        self.assertEqual((body["aggregate_version"], payload["version"]), (2, 2))
        self.assertEqual(payload["event_id"], body["aggregate_id"])
        self.assertEqual(payload["status"], "PUBLISHED")
        self.assertEqual(payload["published_at"], "2026-10-12T23:10:00Z")
        self.assertEqual(payload["updated_at"], payload["published_at"])

    def test_schema_rejects_drafts_invalid_fees_registrations_and_local_instants(self) -> None:
        cases = {
            "borrador": {"status": "DRAFT"},
            "tarifa en otra moneda": {"fee": {"amount": "10000.00", "currency": "USD"}},
            "tarifa con tres decimales": {"fee": {"amount": "1.000", "currency": "COP"}},
            "inscritos": {"registration_count": 3},
            "sin fecha": {"published_at": None},
            "local": {"published_at": "2026-10-12T18:10:00-05:00"},
        }
        for name, change in cases.items():
            body = json.loads(json.dumps(publication_envelope()))
            body["payload"].update(change)
            with self.subTest(case=name):
                self.assertFalse(self.validator.is_valid(body))

    def test_projector_parses_what_the_producer_publishes(self) -> None:
        body = json.loads(json.dumps(publication_envelope()))

        message = parse_sport_event_message(body)

        self.assertIsInstance(message, EventPublishedMessage)
        assert isinstance(message, EventPublishedMessage)
        self.assertEqual(str(message.event.event_id), body["aggregate_id"])
        self.assertIsNotNone(message.event.published_at)

    def test_projector_rejects_what_the_schema_rejects(self) -> None:
        version_one = json.loads(json.dumps(publication_envelope())) | {"aggregate_version": 1}
        self.assertFalse(self.validator.is_valid(version_one))
        # El esquema no puede exigir `aggregate_version == payload.version`; lo hace el proyector.
        for body in (
            version_one,
            json.loads(json.dumps(publication_envelope())) | {"aggregate_version": 3},
        ):
            with self.subTest(version=body["aggregate_version"]), self.assertRaises(InvalidEvent):
                parse_sport_event_message(body)
        body = json.loads(json.dumps(publication_envelope()))
        body["payload"]["status"] = "DRAFT"
        with self.assertRaises(InvalidEvent):
            parse_sport_event_message(body)

    def test_fee_is_part_of_the_full_state_and_optional_for_older_messages(self) -> None:
        """HU025 (por ratificar): el estado completo publicado incluye la tarifa; un mensaje
        anterior sin `fee` sigue siendo válido y equivale a un evento sin tarifa."""
        body = json.loads(json.dumps(publication_envelope()))
        self.assertEqual((body["payload"]["fee"], body["payload"]["fee_updated_at"]), (None, None))
        older = json.loads(json.dumps(publication_envelope()))
        del older["payload"]["fee"], older["payload"]["fee_updated_at"]
        priced = json.loads(json.dumps(publication_envelope()))
        priced["payload"]["fee"] = {"amount": "85000.00", "currency": "COP"}
        priced["payload"]["fee_updated_at"] = "2026-10-12T22:00:00Z"

        for case in (older, priced):
            self.validator.validate(case)
        self.assertIsNone(parse_sport_event_message(older).event.fee)  # type: ignore[union-attr]
        fee = parse_sport_event_message(priced).event.fee  # type: ignore[union-attr]
        self.assertEqual((str(fee.amount), fee.currency) if fee else None, ("85000.00", "COP"))


if __name__ == "__main__":
    unittest.main()
