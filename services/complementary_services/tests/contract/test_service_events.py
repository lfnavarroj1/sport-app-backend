"""El sobre que publica `servicios-outbox-publisher` cumple el esquema versionado de
`servicio.publicado.v1` (ADR-027): payload mínimo, sin nombre, tarifa ni disponibilidad."""

import json
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from complementary_services.api.payload import parse_offer
from complementary_services.domain.catalog.offer import ServiceOffer, validate_offer
from complementary_services.domain.catalog.publication import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    published_payload,
)
from complementary_services.infrastructure.persistence.outbox import ClaimedEvent
from complementary_services.workers.outbox_publisher import build_envelope
from sportapp_platform.testing.contracts import repository_root

SCHEMA_PATH = (
    repository_root(Path(__file__).resolve())
    / "contracts"
    / "events"
    / "servicios"
    / "servicio.publicado.v1.schema.json"
)
NOW = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)
ADR_EXAMPLE = {
    "name": "Valoración deportiva inicial",
    "description": "Valoración presencial para orientar el plan deportivo.",
    "category": "SPORTS_MEDICINE",
    "modality": "IN_PERSON",
    "price": {"amount": "120000.00", "currency": "COP", "billing_unit": "SESSION"},
    "availability": [
        {
            "starts_at": "2026-10-20T09:00:00-05:00",
            "ends_at": "2026-10-20T10:00:00-05:00",
            "time_zone": "America/Bogota",
            "capacity": 1,
        }
    ],
}


def published_envelope() -> dict[str, Any]:
    draft = ServiceOffer.draft(
        service_id=uuid.uuid4(),
        provider_id=uuid.uuid4(),
        offer=validate_offer(parse_offer(ADR_EXAMPLE), NOW),
        now=NOW,
    )
    published = draft.publish(NOW + timedelta(hours=1))
    return build_envelope(
        ClaimedEvent(
            event_id=uuid.uuid4(),
            event_type=EVENT_TYPE,
            schema_version=SCHEMA_VERSION,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=published.service_id,
            aggregate_version=published.version,
            occurred_at=NOW + timedelta(hours=1),
            correlation_id=uuid.uuid4(),
            causation_id=str(uuid.uuid4()),
            payload=published_payload(published),
            attempts=0,
        )
    )


class ServicePublishedContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = Draft202012Validator(
            json.loads(SCHEMA_PATH.read_text(encoding="utf-8")), format_checker=FormatChecker()
        )

    def test_published_envelope_matches_schema(self) -> None:
        body = json.loads(json.dumps(published_envelope()))

        self.validator.validate(body)
        payload = body["payload"]
        self.assertEqual(payload["service_id"], body["aggregate_id"])
        self.assertEqual((payload["status"], body["aggregate_version"]), ("PUBLISHED", 2))
        self.assertEqual(payload["published_at"], "2026-10-12T16:00:00Z")
        self.assertEqual(body["producer"], "complementary-services")

    def test_schema_rejects_offer_or_unknown_data(self) -> None:
        """ADR-027: el evento no transporta nombre, descripción, tarifa ni disponibilidad."""
        extras: list[dict[str, Any]] = [
            {"name": "Valoración deportiva inicial"},
            {"price": {"amount": "120000.00", "currency": "COP", "billing_unit": "SESSION"}},
            {"availability": []},
        ]
        for extra in extras:
            body = json.loads(json.dumps(published_envelope()))
            body["payload"].update(extra)
            with self.subTest(extra=list(extra)):
                self.assertFalse(self.validator.is_valid(body))

    def test_schema_rejects_contract_violations(self) -> None:
        cases: dict[str, tuple[str | None, str, object]] = {
            "draft_status": ("payload", "status", "DRAFT"),
            "local_offset": ("payload", "published_at", "2026-10-12T11:00:00-05:00"),
            "creation_version": (None, "aggregate_version", 1),
            "other_producer": (None, "producer", "events-management"),
            "missing_causation": (None, "causation_id", None),
        }
        for reason, (section, name, value) in cases.items():
            body = json.loads(json.dumps(published_envelope()))
            target = body[section] if section else body
            target[name] = value
            with self.subTest(reason=reason):
                self.assertFalse(self.validator.is_valid(body))


if __name__ == "__main__":
    unittest.main()
