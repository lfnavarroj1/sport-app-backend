"""HU014: el sobre que publica el servicio cumple `plan.alimentacion_creado.v1` (ADR-037) y el
proyector acepta lo que publica el productor."""

import json
import unittest
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from sportapp_platform.testing.contracts import repository_root
from training_management.application.messages import (
    NutritionPlanCreatedEvent,
    parse_training_event,
)
from training_management.domain.nutrition.catalog import FOOD_CATALOG, FOOD_CATALOG_VERSION
from training_management.domain.nutrition.events import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    nutrition_plan_created_payload,
)
from training_management.domain.nutrition.model import Meal, MealType, NutritionPlan, PlanItem
from training_management.infrastructure.persistence_write.outbox import ClaimedEvent
from training_management.workers.outbox_publisher import build_envelope

SCHEMA = (
    repository_root(Path(__file__).resolve())
    / "contracts"
    / "events"
    / "entrenamientos"
    / f"{EVENT_TYPE}.schema.json"
)
NOW = datetime(2026, 10, 22, 15, 30, tzinfo=UTC)


def nutrition_plan() -> NutritionPlan:
    return NutritionPlan.create(
        plan_id=uuid.uuid4(),
        athlete_id=uuid.uuid4(),
        professional_id=uuid.uuid4(),
        title="Recuperación de pretemporada",
        objectives=["Hidratación diaria", "Proteína después de entrenar"],
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 30),
        meals=[
            Meal(
                MealType.BREAKFAST,
                (PlanItem("OATS", Decimal("80"), "G"), PlanItem("BANANA", Decimal("1"), "UNIT")),
            ),
            Meal(MealType.HYDRATION, (PlanItem("WATER", Decimal("500.0"), "ML"),)),
        ],
        catalog=FOOD_CATALOG,
        now=NOW,
    )


def published(plan: NutritionPlan) -> dict[str, Any]:
    envelope = build_envelope(
        ClaimedEvent(
            event_id=uuid.uuid4(),
            event_type=EVENT_TYPE,
            schema_version=SCHEMA_VERSION,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=plan.plan_id,
            aggregate_version=plan.version,
            occurred_at=plan.created_at,
            correlation_id=uuid.uuid4(),
            causation_id=str(uuid.uuid4()),
            payload=nutrition_plan_created_payload(plan),
            attempts=0,
        )
    )
    # Ida y vuelta por JSON: así viaja por Pub/Sub.
    loaded: dict[str, Any] = json.loads(json.dumps(envelope))
    return loaded


class NutritionPlanCreatedEventContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = Draft202012Validator(
            json.loads(SCHEMA.read_text(encoding="utf-8")), format_checker=FormatChecker()
        )
        self.plan = nutrition_plan()

    def test_published_envelope_matches_the_schema(self) -> None:
        body = published(self.plan)

        self.validator.validate(body)
        self.assertEqual(
            (body["aggregate_type"], body["aggregate_id"], body["aggregate_version"]),
            ("nutrition_plan", str(self.plan.plan_id), 1),
        )
        payload = body["payload"]
        self.assertEqual((payload["status"], payload["version"]), ("ASSIGNED", 1))
        self.assertEqual(payload["catalog_version"], FOOD_CATALOG_VERSION)
        self.assertEqual(
            payload["meals"][0]["items"],
            [
                {"food_code": "OATS", "quantity": "80.0", "unit": "G"},
                {"food_code": "BANANA", "quantity": "1.0", "unit": "UNIT"},
            ],
        )
        self.assertTrue(payload["created_at"].endswith("Z"))

    def test_schema_rejects_restrictions_warnings_and_free_text(self) -> None:
        """Las alergias y restricciones consultadas en Usuarios nunca viajan en el evento."""
        for extra in ("allergies", "restrictions", "diet_pattern", "warnings", "notes"):
            body = published(self.plan)
            body["payload"][extra] = ["dato no autorizado"]
            with self.subTest(field=extra):
                self.assertFalse(self.validator.is_valid(body))

    def test_schema_rejects_malformed_items(self) -> None:
        for field, value in (
            ("quantity", 80),
            ("quantity", "80"),
            ("quantity", "80.25"),
            ("unit", "KG"),
            ("food_code", "avena"),
        ):
            body = published(self.plan)
            body["payload"]["meals"][0]["items"][0][field] = value
            with self.subTest(field=field, value=value):
                self.assertFalse(self.validator.is_valid(body))

    def test_projector_accepts_what_the_producer_publishes(self) -> None:
        event = parse_training_event(published(self.plan))

        assert isinstance(event, NutritionPlanCreatedEvent)
        self.assertEqual(event.plan, self.plan)


if __name__ == "__main__":
    unittest.main()
