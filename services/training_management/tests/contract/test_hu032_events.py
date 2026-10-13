"""HU032: los sobres que produce el servicio cumplen `entrenamiento.iniciado.v1` (nuevo) y el
esquema **sin cambios** de `entrenamiento.finalizado.v1` (HU011), y el proyector los acepta."""

import json
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from sportapp_platform.testing.contracts import repository_root
from training_management.application.messages import (
    SessionFinishedEvent,
    SessionStartedEvent,
    parse_training_event,
)
from training_management.domain.plans.model import Sport
from training_management.domain.sessions.events import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    STARTED_EVENT_TYPE,
    session_finished_payload,
    session_started_payload,
)
from training_management.domain.sessions.model import StartData, TrainingSession
from training_management.infrastructure.persistence_write.outbox import ClaimedEvent
from training_management.workers.outbox_publisher import build_envelope

SCHEMAS = repository_root(Path(__file__).resolve()) / "contracts" / "events" / "entrenamientos"
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def validator(event_type: str) -> Draft202012Validator:
    return Draft202012Validator(
        json.loads((SCHEMAS / f"{event_type}.schema.json").read_text(encoding="utf-8")),
        format_checker=FormatChecker(),
    )


def published(session: TrainingSession, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    envelope = build_envelope(
        ClaimedEvent(
            event_id=uuid.uuid4(),
            event_type=event_type,
            schema_version=SCHEMA_VERSION,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=session.session_id,
            aggregate_version=session.version,
            occurred_at=session.updated_at,
            correlation_id=uuid.uuid4(),
            causation_id=str(uuid.uuid4()),
            payload=payload,
            attempts=0,
        )
    )
    loaded: dict[str, Any] = json.loads(json.dumps(envelope))
    return loaded


class TrainingSessionEventsContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.started = TrainingSession.start(
            athlete_id=uuid.uuid4(),
            data=StartData(
                session_id=uuid.uuid4(),
                plan_id=uuid.uuid4(),
                sport=Sport.CYCLING,
                started_at=datetime(2026, 10, 12, 11, 0, tzinfo=UTC),
            ),
            now=NOW,
        )
        self.finished = self.started.finish(
            ended_at=datetime(2026, 10, 12, 11, 40, 5, tzinfo=UTC), now=NOW
        )

    def test_started_event_matches_its_schema(self) -> None:
        body = published(self.started, STARTED_EVENT_TYPE, session_started_payload(self.started))

        validator(STARTED_EVENT_TYPE).validate(body)
        self.assertEqual((body["aggregate_version"], body["payload"]["status"]), (1, "ACTIVE"))
        self.assertIsInstance(parse_training_event(body), SessionStartedEvent)

    def test_finished_event_matches_the_unchanged_hu011_schema(self) -> None:
        body = published(self.finished, EVENT_TYPE, session_finished_payload(self.finished))

        validator(EVENT_TYPE).validate(body)
        self.assertEqual(body["aggregate_version"], 2)
        self.assertEqual(body["payload"]["duration_seconds"], 2405)
        event = parse_training_event(body)
        assert isinstance(event, SessionFinishedEvent)
        self.assertEqual(event.session.duration_seconds, 2405)

    def test_schemas_reject_health_or_location_data(self) -> None:
        for session, event_type, payload in (
            (self.started, STARTED_EVENT_TYPE, session_started_payload(self.started)),
            (self.finished, EVENT_TYPE, session_finished_payload(self.finished)),
        ):
            body = published(session, event_type, payload)
            body["payload"]["heart_rate"] = 150
            with self.subTest(event_type=event_type):
                self.assertFalse(validator(event_type).is_valid(body))


if __name__ == "__main__":
    unittest.main()
