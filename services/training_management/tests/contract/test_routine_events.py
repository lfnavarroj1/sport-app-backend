"""HU030: los sobres que publica el servicio cumplen `plantilla_rutina.publicada.v1` y
`.retirada.v1` (ADR-041) y el proyector acepta lo que publica el productor."""

import json
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from sportapp_platform.testing.contracts import repository_root
from training_management.application.messages import RoutineChangedEvent, parse_training_event
from training_management.domain.nutrition.catalog import FOOD_CATALOG
from training_management.domain.nutrition.model import MealType
from training_management.domain.routines.events import (
    AGGREGATE_TYPE,
    EVENT_TYPE_BY_STATUS,
    PUBLISHED_EVENT_TYPE,
    RETIRED_EVENT_TYPE,
    SCHEMA_VERSION,
    routine_document,
)
from training_management.domain.routines.model import (
    Allergy,
    DietPattern,
    NutritionRoutineTemplate,
    RoutineInput,
    RoutineItem,
    RoutineMeal,
)
from training_management.domain.templates.model import Goal
from training_management.infrastructure.persistence_write.outbox import ClaimedEvent
from training_management.workers.outbox_publisher import build_envelope

SCHEMAS = repository_root(Path(__file__).resolve()) / "contracts" / "events" / "entrenamientos"
NOW = datetime(2026, 10, 25, 15, 30, tzinfo=UTC)


def validator(event_type: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / f"{event_type}.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def routine() -> NutritionRoutineTemplate:
    return NutritionRoutineTemplate.publish(
        template_id=uuid.uuid4(),
        professional_id=uuid.uuid4(),
        data=RoutineInput(
            title="Recuperación pescetariana",
            description="Comidas para los días posteriores a una competencia.",
            goal=Goal.COMPETITION,
            sport=None,
            dietary_pattern=DietPattern.PESCATARIAN,
            contains_allergens=(Allergy.SESAME,),
            duration_days=7,
            daily_meals=(
                RoutineMeal(MealType.BREAKFAST, (RoutineItem("GREEK_YOGURT", 1),)),
                RoutineMeal(MealType.DINNER, (RoutineItem("TUNA", 2), RoutineItem("QUINOA", 1))),
            ),
        ),
        catalog=FOOD_CATALOG,
        now=NOW,
    )


def published(state: NutritionRoutineTemplate) -> dict[str, Any]:
    envelope = build_envelope(
        ClaimedEvent(
            event_id=uuid.uuid4(),
            event_type=EVENT_TYPE_BY_STATUS[state.status],
            schema_version=SCHEMA_VERSION,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=state.template_id,
            aggregate_version=state.version,
            occurred_at=state.updated_at,
            correlation_id=uuid.uuid4(),
            causation_id=str(uuid.uuid4()),
            payload=routine_document(state),
            attempts=0,
        )
    )
    loaded: dict[str, Any] = json.loads(json.dumps(envelope))
    return loaded


class RoutineEventsContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.routine = routine()
        self.retired = self.routine.retire(NOW + timedelta(days=1))

    def test_envelopes_match_their_schemas(self) -> None:
        for state, event_type, version in (
            (self.routine, PUBLISHED_EVENT_TYPE, 1),
            (self.retired, RETIRED_EVENT_TYPE, 2),
        ):
            with self.subTest(event_type=event_type):
                body = published(state)

                validator(event_type).validate(body)
                self.assertEqual(
                    (body["aggregate_type"], body["aggregate_version"]),
                    ("nutrition_routine_template", version),
                )
                self.assertEqual(body["aggregate_id"], body["payload"]["template_id"])
                self.assertEqual(body["payload"]["contains_allergens"], ["MILK", "FISH", "SESAME"])
        self.assertFalse(validator(PUBLISHED_EVENT_TYPE).is_valid(published(self.retired)))
        self.assertFalse(validator(RETIRED_EVENT_TYPE).is_valid(published(self.routine)))

    def test_schema_rejects_athlete_data_and_malformed_values(self) -> None:
        """Sin datos del deportista (ni sus alergias), sin precio ni entrega (CA5)."""
        check = validator(PUBLISHED_EVENT_TYPE)
        for field, value in (
            ("athlete_id", str(uuid.uuid4())),
            ("allergies", ["FISH"]),
            ("price", "10000"),
            ("duration_days", 91),
            ("dietary_pattern", "PALEO"),
            ("contains_allergens", ["FISH", "FISH"]),
            ("daily_meals", []),
        ):
            body = published(self.routine)
            body["payload"][field] = value
            with self.subTest(field=field):
                self.assertFalse(check.is_valid(body))

    def test_projector_accepts_what_the_producer_publishes(self) -> None:
        for state in (self.routine, self.retired):
            with self.subTest(status=state.status):
                event = parse_training_event(published(state))

                assert isinstance(event, RoutineChangedEvent)
                self.assertEqual(event.routine, state)


if __name__ == "__main__":
    unittest.main()
