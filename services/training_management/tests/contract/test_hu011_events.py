"""HU011: contratos de `autorizacion_entrenamiento.*.v1` y `entrenamiento.finalizado.v1`.

Los eventos de autorización que produce el servicio cumplen su esquema y el
proyector los acepta. `entrenamiento.finalizado.v1` todavía no tiene productor
(HU032): se verifica que el esquema y el proyector acepten el mismo sobre.
"""

import json
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from sportapp_platform.testing.contracts import repository_root
from training_management.application.commands.authorizations import authorization_changed
from training_management.application.messages import (
    AuthorizationChangedEvent,
    SessionFinishedEvent,
    parse_training_event,
)
from training_management.domain.authorizations.model import TrainingAuthorization
from training_management.infrastructure.persistence_write.outbox import ClaimedEvent
from training_management.workers.outbox_publisher import build_envelope

SCHEMAS = repository_root(Path(__file__).resolve()) / "contracts" / "events" / "entrenamientos"
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def validator(event_type: str) -> Draft202012Validator:
    return Draft202012Validator(
        json.loads((SCHEMAS / f"{event_type}.schema.json").read_text(encoding="utf-8")),
        format_checker=FormatChecker(),
    )


def published(authorization: TrainingAuthorization) -> dict[str, Any]:
    message = authorization_changed(
        authorization, event_id=uuid.uuid4(), now=NOW, correlation_id=uuid.uuid4()
    )
    envelope = build_envelope(
        ClaimedEvent(
            event_id=message.event_id,
            event_type=message.event_type,
            schema_version=message.schema_version,
            aggregate_type=message.aggregate_type,
            aggregate_id=message.aggregate_id,
            aggregate_version=message.aggregate_version,
            occurred_at=message.occurred_at,
            correlation_id=message.correlation_id,
            causation_id=message.causation_id,
            payload=message.payload,
            attempts=0,
        )
    )
    loaded: dict[str, Any] = json.loads(json.dumps(envelope))
    return loaded


class AuthorizationEventsContractTest(unittest.TestCase):
    def test_granted_and_revoked_envelopes_match_their_schemas(self) -> None:
        granted = TrainingAuthorization.grant(
            athlete_id=uuid.uuid4(), professional_id=uuid.uuid4(), now=NOW
        )
        revoked = granted.revoke(NOW + timedelta(hours=1))

        for authorization in (granted, revoked):
            body = published(authorization)
            with self.subTest(event=body["event_type"]):
                validator(body["event_type"]).validate(body)
                event = parse_training_event(body)
                assert isinstance(event, AuthorizationChangedEvent)
                self.assertEqual(event.authorization.version, authorization.version)

    def test_schema_rejects_profile_data(self) -> None:
        body = published(
            TrainingAuthorization.grant(
                athlete_id=uuid.uuid4(), professional_id=uuid.uuid4(), now=NOW
            )
        )
        body["payload"]["professional_email"] = "pro@example.com"

        self.assertFalse(validator(body["event_type"]).is_valid(body))


class SessionFinishedContractTest(unittest.TestCase):
    def envelope(self) -> dict[str, Any]:
        session_id = str(uuid.uuid4())
        return {
            "event_id": str(uuid.uuid4()),
            "event_type": "entrenamiento.finalizado.v1",
            "occurred_at": NOW.isoformat(),
            "producer": "training-management",
            "aggregate_type": "training_session",
            "aggregate_id": session_id,
            "aggregate_version": 2,
            "correlation_id": None,
            "causation_id": None,
            "schema_version": 1,
            "payload": {
                "session_id": session_id,
                "athlete_id": str(uuid.uuid4()),
                "plan_id": str(uuid.uuid4()),
                "sport": "swimming",
                "started_at": (NOW - timedelta(hours=1)).isoformat(),
                "ended_at": NOW.isoformat(),
                "duration_seconds": 3600,
                "status": "FINISHED",
            },
        }

    def test_schema_and_projector_accept_the_same_envelope(self) -> None:
        body = self.envelope()

        validator("entrenamiento.finalizado.v1").validate(body)
        event = parse_training_event(body)

        assert isinstance(event, SessionFinishedEvent)
        self.assertEqual(str(event.session.session_id), body["aggregate_id"])

    def test_schema_rejects_health_data(self) -> None:
        body = self.envelope()
        body["payload"]["heart_rate_avg"] = 150

        self.assertFalse(validator("entrenamiento.finalizado.v1").is_valid(body))


if __name__ == "__main__":
    unittest.main()
