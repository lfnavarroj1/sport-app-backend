"""El sobre que publica `eventos-outbox-publisher` al cambiar la tarifa cumple el esquema
versionado de `evento.tarifa_actualizada.v1` (HU025, ADR-040), y el proyector acepta
exactamente ese contrato."""

import json
import unittest
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from test_events import ADR_EXAMPLE, NOW

from events_management.api.payload import parse_event
from events_management.application.messages import (
    EventFeeUpdatedMessage,
    InvalidEvent,
    parse_sport_event_message,
)
from events_management.domain.sport_events.fee import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    EventFee,
    FeeChange,
)
from events_management.domain.sport_events.model import SportEvent, validate_event
from events_management.domain.sport_events.published import aggregate_document
from events_management.infrastructure.persistence_write.outbox import ClaimedEvent
from events_management.workers.outbox_publisher import build_envelope
from sportapp_platform.testing.contracts import repository_root

SCHEMA_PATH = (
    repository_root(Path(__file__).resolve())
    / "contracts"
    / "events"
    / "eventos"
    / "evento.tarifa_actualizada.v1.schema.json"
)
FEE = EventFee(Decimal("85000.00"), "COP")


def fee_envelope(*, publish: bool = False, fee: EventFee | None = FEE) -> dict[str, object]:
    event = SportEvent.draft(
        event_id=uuid.uuid4(),
        organizer_id=uuid.uuid4(),
        details=validate_event(parse_event(ADR_EXAMPLE), NOW),
        now=NOW,
    )
    if publish:
        event = event.publish(NOW + timedelta(minutes=5))
    event = event.change_fee(FeeChange(fee, event.version), NOW + timedelta(minutes=10))
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


def body_of(envelope: dict[str, object]) -> dict[str, object]:
    document: dict[str, object] = json.loads(json.dumps(envelope))
    return document


class EventFeeUpdatedContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = Draft202012Validator(
            json.loads(SCHEMA_PATH.read_text(encoding="utf-8")), format_checker=FormatChecker()
        )

    def test_envelope_matches_schema_with_full_state_and_fee(self) -> None:
        for publish in (False, True):
            with self.subTest(published=publish):
                body = body_of(fee_envelope(publish=publish))
                self.validator.validate(body)
                payload = body["payload"]
                assert isinstance(payload, dict)
                self.assertEqual(body["event_type"], "evento.tarifa_actualizada.v1")
                self.assertEqual(body["aggregate_version"], payload["version"])
                self.assertEqual(payload["event_id"], body["aggregate_id"])
                self.assertEqual(payload["fee"], {"amount": "85000.00", "currency": "COP"})
                self.assertEqual(payload["fee_updated_at"], "2026-10-12T21:10:00Z")
                self.assertEqual(payload["status"], "PUBLISHED" if publish else "DRAFT")

    def test_free_event_and_removed_fee_are_valid(self) -> None:
        """`0.00` es gratuito; retirar deja `fee = null` con `fee_updated_at`."""
        free = body_of(fee_envelope(fee=EventFee(Decimal("0.00"), "COP")))
        removed = body_of(fee_envelope(fee=None))

        for body in (free, removed):
            self.validator.validate(body)
        self.assertEqual(free["payload"]["fee"]["amount"], "0.00")  # type: ignore[index]
        self.assertIsNone(removed["payload"]["fee"])  # type: ignore[index]
        self.assertIsNotNone(removed["payload"]["fee_updated_at"])  # type: ignore[index]

    def test_schema_rejects_payments_counts_other_currencies_and_bad_amounts(self) -> None:
        cases = {
            "otra moneda": {"fee": {"amount": "10.00", "currency": "USD"}},
            "tres decimales": {"fee": {"amount": "10.000", "currency": "COP"}},
            "negativo": {"fee": {"amount": "-1.00", "currency": "COP"}},
            "fuera de rango": {"fee": {"amount": "100000000.00", "currency": "COP"}},
            "estado de pago": {"payment_status": "PAID"},
            "inscritos": {"registration_count": 3},
            "sin tarifa": {"fee": "missing"},
        }
        for name, change in cases.items():
            body = body_of(fee_envelope())
            payload = body["payload"]
            assert isinstance(payload, dict)
            if change.get("fee") == "missing":
                del payload["fee"]
            else:
                payload.update(change)
            with self.subTest(case=name):
                self.assertFalse(self.validator.is_valid(body))

    def test_projector_parses_what_the_producer_publishes(self) -> None:
        body = body_of(fee_envelope(publish=True))

        message = parse_sport_event_message(body)

        self.assertIsInstance(message, EventFeeUpdatedMessage)
        assert isinstance(message, EventFeeUpdatedMessage)
        self.assertEqual(message.event.fee, FEE)
        self.assertEqual(message.aggregate_version, 3)
        self.assertIsNotNone(message.event.published_at)

    def test_projector_rejects_invalid_messages(self) -> None:
        cases = {
            "versión 1": lambda b: b.update({"aggregate_version": 1}),
            "otra moneda": lambda b: b["payload"].update(
                {"fee": {"amount": "10.00", "currency": "USD"}}
            ),
            "sin fee": lambda b: b["payload"].pop("fee"),
            "tarifa sin instante": lambda b: b["payload"].update({"fee_updated_at": None}),
            "borrador con fecha de publicación": lambda b: b["payload"].update(
                {"published_at": "2026-10-12T21:05:00Z"}
            ),
        }
        for name, change in cases.items():
            body = body_of(fee_envelope())
            change(body)
            with self.subTest(case=name), self.assertRaises(InvalidEvent):
                parse_sport_event_message(body)


if __name__ == "__main__":
    unittest.main()
