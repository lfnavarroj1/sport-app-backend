"""HU028: los sobres que publica el servicio cumplen `plantilla_entrenamiento.publicada.v1` y
`.retirada.v1` (ADR-041) y el proyector acepta lo que publica el productor."""

import json
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from sportapp_platform.testing.contracts import repository_root
from training_management.application.messages import TemplateChangedEvent, parse_training_event
from training_management.domain.plans.model import Sport
from training_management.domain.templates.events import (
    AGGREGATE_TYPE,
    EVENT_TYPE_BY_STATUS,
    PUBLISHED_EVENT_TYPE,
    RETIRED_EVENT_TYPE,
    SCHEMA_VERSION,
    template_document,
)
from training_management.domain.templates.model import (
    DayOfWeek,
    Goal,
    Level,
    TemplateInput,
    TrainingPlanTemplate,
    WeeklySession,
)
from training_management.infrastructure.persistence_write.outbox import ClaimedEvent
from training_management.workers.outbox_publisher import build_envelope

SCHEMAS = repository_root(Path(__file__).resolve()) / "contracts" / "events" / "entrenamientos"
NOW = datetime(2026, 10, 24, 15, 30, tzinfo=UTC)


def validator(event_type: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / f"{event_type}.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def template() -> TrainingPlanTemplate:
    return TrainingPlanTemplate.publish(
        template_id=uuid.uuid4(),
        professional_id=uuid.uuid4(),
        data=TemplateInput(
            title="Base de ciclismo",
            objectives="Sumar volumen aeróbico durante seis semanas.",
            sport=Sport.CYCLING,
            goal=Goal.HEALTH,
            level=Level.INTERMEDIATE,
            duration_weeks=6,
            weekly_sessions=(
                WeeklySession(DayOfWeek.TUE, "Rodillo suave", 60),
                WeeklySession(DayOfWeek.SAT, "Fondo", 180),
            ),
        ),
        now=NOW,
    )


def published(state: TrainingPlanTemplate) -> dict[str, Any]:
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
            payload=template_document(state),
            attempts=0,
        )
    )
    loaded: dict[str, Any] = json.loads(json.dumps(envelope))
    return loaded


class TemplateEventsContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.template = template()
        self.retired = self.template.retire(NOW + timedelta(days=1))

    def test_envelopes_match_their_schemas(self) -> None:
        for state, event_type, version in (
            (self.template, PUBLISHED_EVENT_TYPE, 1),
            (self.retired, RETIRED_EVENT_TYPE, 2),
        ):
            with self.subTest(event_type=event_type):
                body = published(state)

                validator(event_type).validate(body)
                self.assertEqual(
                    (body["aggregate_type"], body["aggregate_version"]),
                    ("training_plan_template", version),
                )
                self.assertEqual(body["aggregate_id"], body["payload"]["template_id"])
        self.assertFalse(validator(PUBLISHED_EVENT_TYPE).is_valid(published(self.retired)))
        self.assertFalse(validator(RETIRED_EVENT_TYPE).is_valid(published(self.template)))

    def test_schema_rejects_athlete_data_and_malformed_values(self) -> None:
        check = validator(PUBLISHED_EVENT_TYPE)
        for field, value in (
            ("athlete_id", str(uuid.uuid4())),
            ("start_date", "2026-11-02"),
            ("duration_weeks", 53),
            ("goal", "FUN"),
            ("weekly_sessions", []),
        ):
            body = published(self.template)
            body["payload"][field] = value
            with self.subTest(field=field):
                self.assertFalse(check.is_valid(body))

    def test_projector_accepts_what_the_producer_publishes(self) -> None:
        for state in (self.template, self.retired):
            with self.subTest(status=state.status):
                event = parse_training_event(published(state))

                assert isinstance(event, TemplateChangedEvent)
                self.assertEqual(event.template, state)


if __name__ == "__main__":
    unittest.main()
