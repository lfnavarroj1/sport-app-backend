"""HU043: el sobre que publica el servicio cumple `consumo_alimento.registrado.v1` (ADR-037) y
el proyector acepta lo que publica el productor."""

import json
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from sportapp_platform.testing.contracts import repository_root
from training_management.application.messages import (
    FoodIntakeRegisteredEvent,
    parse_training_event,
)
from training_management.domain.nutrition.catalog import FOOD_CATALOG, FOOD_CATALOG_VERSION
from training_management.domain.nutrition.intake import FoodIntake, validate_food_intake
from training_management.domain.nutrition.intake_events import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    food_intake_registered_payload,
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
NOW = datetime(2026, 10, 24, 15, 30, tzinfo=UTC)


def food_intake() -> FoodIntake:
    data = validate_food_intake(
        {
            "intake_id": str(uuid.uuid4()),
            "consumed_at": "2026-10-24T12:30:00Z",
            "meal_type": "LUNCH",
            "items": [
                {"food_code": "WHITE_RICE", "quantity": "150", "unit": "G"},
                {"food_code": "CHICKEN_BREAST", "quantity": "120.5", "unit": "G"},
                {"food_code": "WATER", "quantity": "1", "unit": "CUP"},
            ],
        },
        FOOD_CATALOG,
        NOW,
    )
    return FoodIntake.register(athlete_id=uuid.uuid4(), data=data, catalog=FOOD_CATALOG, now=NOW)


def published(intake: FoodIntake) -> dict[str, Any]:
    envelope = build_envelope(
        ClaimedEvent(
            event_id=uuid.uuid4(),
            event_type=EVENT_TYPE,
            schema_version=SCHEMA_VERSION,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=intake.intake_id,
            aggregate_version=intake.version,
            occurred_at=intake.registered_at,
            correlation_id=uuid.uuid4(),
            causation_id=str(uuid.uuid4()),
            payload=food_intake_registered_payload(intake),
            attempts=0,
        )
    )
    # Ida y vuelta por JSON: así viaja por Pub/Sub.
    loaded: dict[str, Any] = json.loads(json.dumps(envelope))
    return loaded


class FoodIntakeRegisteredEventContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = Draft202012Validator(
            json.loads(SCHEMA.read_text(encoding="utf-8")), format_checker=FormatChecker()
        )
        self.intake = food_intake()

    def test_published_envelope_matches_the_schema(self) -> None:
        body = published(self.intake)

        self.validator.validate(body)
        self.assertEqual(
            (body["aggregate_type"], body["aggregate_id"], body["aggregate_version"]),
            ("food_intake", str(self.intake.intake_id), 1),
        )
        payload = body["payload"]
        self.assertEqual(
            (payload["version"], payload["catalog_version"]), (1, FOOD_CATALOG_VERSION)
        )
        self.assertEqual(
            payload["items"],
            [
                {"food_code": "WHITE_RICE", "quantity": "150.0", "unit": "G"},
                {"food_code": "CHICKEN_BREAST", "quantity": "120.5", "unit": "G"},
                {"food_code": "WATER", "quantity": "1.0", "unit": "CUP"},
            ],
        )
        self.assertEqual(payload["consumed_at"], "2026-10-24T12:30:00Z")
        self.assertTrue(payload["registered_at"].endswith("Z"))

    def test_schema_rejects_nutrients_and_free_text(self) -> None:
        """Sin calorías, nutrientes ni texto libre (ADR-037)."""
        for extra in ("calories", "nutrients", "notes", "athlete_name", "email"):
            body = published(self.intake)
            body["payload"][extra] = "dato no autorizado"
            with self.subTest(field=extra):
                self.assertFalse(self.validator.is_valid(body))

    def test_schema_rejects_malformed_items(self) -> None:
        for field, value in (
            ("quantity", 80),
            ("quantity", "80"),
            ("quantity", "80.25"),
            ("unit", "KG"),
            ("food_code", "arroz"),
        ):
            body = published(self.intake)
            body["payload"]["items"][0][field] = value
            with self.subTest(field=field, value=value):
                self.assertFalse(self.validator.is_valid(body))
        body = published(self.intake)
        body["payload"]["items"] = body["payload"]["items"] * 7
        self.assertFalse(self.validator.is_valid(body), "más de 20 ítems")

    def test_projector_accepts_what_the_producer_publishes(self) -> None:
        event = parse_training_event(published(self.intake))

        assert isinstance(event, FoodIntakeRegisteredEvent)
        self.assertEqual(event.intake, self.intake)


if __name__ == "__main__":
    unittest.main()
