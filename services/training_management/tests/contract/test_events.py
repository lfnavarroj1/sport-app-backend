"""El sobre que publica `entrenamientos-outbox-publisher` cumple el esquema versionado
de `plan.entrenamiento_creado.v1`, y el proyector acepta exactamente ese contrato."""

import json
import unittest
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from sportapp_platform.testing.contracts import repository_root
from training_management.application.messages import parse_training_event
from training_management.domain.plans.events import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    plan_created_payload,
)
from training_management.domain.plans.model import PlannedSession, Sport, TrainingPlan
from training_management.infrastructure.persistence_write.outbox import ClaimedEvent
from training_management.workers.outbox_publisher import build_envelope

SCHEMA_PATH = (
    repository_root(Path(__file__).resolve())
    / "contracts"
    / "events"
    / "entrenamientos"
    / "plan.entrenamiento_creado.v1.schema.json"
)
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def published_envelope() -> dict[str, object]:
    plan = TrainingPlan.create(
        plan_id=uuid.uuid4(),
        athlete_id=uuid.uuid4(),
        professional_id=uuid.uuid4(),
        title="Base aeróbica",
        objectives=["Correr 10 km", "Mejorar la cadencia"],
        start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 29),
        sessions=[
            PlannedSession(date(2026, 11, 2), Sport.RUNNING, 40, 5),
            PlannedSession(date(2026, 11, 4), Sport.STRENGTH, 30, 6),
        ],
        now=NOW,
    )
    return build_envelope(
        ClaimedEvent(
            event_id=uuid.uuid4(),
            event_type=EVENT_TYPE,
            schema_version=SCHEMA_VERSION,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=plan.plan_id,
            aggregate_version=plan.version,
            occurred_at=NOW,
            correlation_id=uuid.uuid4(),
            causation_id=str(uuid.uuid4()),
            payload=plan_created_payload(plan),
            attempts=0,
        )
    )


class PlanCreatedEventContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = Draft202012Validator(
            json.loads(SCHEMA_PATH.read_text(encoding="utf-8")), format_checker=FormatChecker()
        )

    def test_published_envelope_matches_schema(self) -> None:
        body = json.loads(json.dumps(published_envelope()))

        self.validator.validate(body)
        self.assertEqual(body["payload"]["plan_id"], body["aggregate_id"])

    def test_schema_rejects_profile_data_in_payload(self) -> None:
        body = json.loads(json.dumps(published_envelope()))
        body["payload"]["athlete_email"] = "ana@example.com"

        self.assertFalse(self.validator.is_valid(body))

    def test_projector_parses_what_the_producer_publishes(self) -> None:
        body = json.loads(json.dumps(published_envelope()))

        event = parse_training_event(body)

        self.assertEqual(str(event.plan.plan_id), body["aggregate_id"])
        self.assertEqual(len(event.plan.sessions), 2)


if __name__ == "__main__":
    unittest.main()
