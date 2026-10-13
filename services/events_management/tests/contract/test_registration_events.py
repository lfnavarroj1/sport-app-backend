"""El sobre que publica `eventos-outbox-publisher` cumple `usuario.inscrito.v1` (ADR-032) y el
proyector acepta exactamente ese contrato."""

import json
import unittest
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from events_management.application.messages import (
    RegistrationCreatedMessage,
    parse_sport_event_message,
)
from events_management.domain.registrations.events import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    registered_payload,
)
from events_management.domain.registrations.model import (
    AppliedFee,
    EventRegistration,
    RegistrationStatus,
)
from events_management.infrastructure.persistence_write.outbox import ClaimedEvent
from events_management.workers.outbox_publisher import build_envelope
from sportapp_platform.testing.contracts import repository_root

SCHEMA = (
    repository_root(Path(__file__).resolve())
    / "contracts"
    / "events"
    / "eventos"
    / "usuario.inscrito.v1.schema.json"
)
NOW = datetime(2026, 10, 12, 22, 30, tzinfo=UTC)


def published(fee: AppliedFee | None = None) -> dict[str, Any]:
    registration = EventRegistration(
        registration_id=uuid.uuid4(),
        event_id=uuid.uuid4(),
        athlete_id=uuid.uuid4(),
        status=RegistrationStatus.REGISTERED,
        registered_at=NOW,
        applied_fee=fee,
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    envelope = build_envelope(
        ClaimedEvent(
            event_id=uuid.uuid4(),
            event_type=EVENT_TYPE,
            schema_version=SCHEMA_VERSION,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=registration.registration_id,
            aggregate_version=1,
            occurred_at=NOW,
            correlation_id=uuid.uuid4(),
            causation_id=str(uuid.uuid4()),
            payload=registered_payload(registration),
            attempts=0,
        )
    )
    loaded: dict[str, Any] = json.loads(json.dumps(envelope))
    return loaded


class RegistrationEventContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = Draft202012Validator(
            json.loads(SCHEMA.read_text(encoding="utf-8")), format_checker=FormatChecker()
        )

    def test_envelope_with_and_without_fee_matches_schema(self) -> None:
        for fee in (None, AppliedFee(Decimal("50000"), "COP")):
            body = published(fee)
            with self.subTest(fee=fee):
                self.validator.validate(body)
                self.assertEqual(body["payload"]["registration_id"], body["aggregate_id"])
        self.assertEqual(
            published(AppliedFee(Decimal("50000"), "COP"))["payload"]["applied_fee"]["amount"],
            "50000.00",
        )

    def test_schema_rejects_event_or_payment_data(self) -> None:
        for extra in ({"event_name": "Carrera"}, {"payment_status": "PAID"}):
            body = published()
            body["payload"].update(extra)
            with self.subTest(extra=extra):
                self.assertFalse(self.validator.is_valid(body))

    def test_projector_parses_what_the_producer_publishes(self) -> None:
        body = published()

        message = parse_sport_event_message(body)

        assert isinstance(message, RegistrationCreatedMessage)
        self.assertEqual(str(message.registration.registration_id), body["aggregate_id"])


if __name__ == "__main__":
    unittest.main()
