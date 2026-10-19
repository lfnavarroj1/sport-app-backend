"""HU032 CA8: el proyector crea y retira la sesión activa y alimenta el historial una sola vez,
aunque Pub/Sub repita o desordene las entregas."""

import unittest
from typing import Any
from uuid import uuid4

from fakes import NOW, FixedClock, InMemoryProjectionUnitOfWork, InMemoryReadStore
from session_fakes import start_body
from test_workers import Message

from training_management.api.session_payload import parse_start
from training_management.application.messages import (
    InvalidEvent,
    SessionStartedEvent,
    parse_training_event,
)
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.application.projections.snapshot_projection import TrainingProjector
from training_management.domain.sessions.events import (
    session_finished_payload,
    session_started_payload,
)
from training_management.domain.sessions.model import TrainingSession, validate_start
from training_management.workers.read_projector import ProjectionMessageHandler


def a_session() -> TrainingSession:
    return TrainingSession.start(
        athlete_id=uuid4(), data=validate_start(parse_start(start_body()), NOW), now=NOW
    )


def envelope(session: TrainingSession, *, finished: bool = False, **overrides: Any) -> dict:
    if finished:
        session = session.finish(ended_at=NOW, now=NOW)
    values: dict[str, Any] = {
        "event_id": str(uuid4()),
        "event_type": "entrenamiento.finalizado.v1" if finished else "entrenamiento.iniciado.v1",
        "occurred_at": "2026-10-12T12:00:00Z",
        "producer": "training-management",
        "aggregate_type": "training_session",
        "aggregate_id": str(session.session_id),
        "aggregate_version": session.version,
        "correlation_id": None,
        "causation_id": str(uuid4()),
        "schema_version": 1,
        "payload": session_finished_payload(session)
        if finished
        else session_started_payload(session),
    }
    values.update(overrides)
    return values


class SessionProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.read = InMemoryReadStore()
        self.project = TrainingProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.read), clock=FixedClock()
        )

    def test_start_then_finish(self) -> None:
        session = a_session()
        started = parse_training_event(envelope(session))

        self.assertIsInstance(started, SessionStartedEvent)
        self.assertEqual(self.project(started), ProjectionOutcome.PROJECTED)
        self.assertEqual(self.project(started), ProjectionOutcome.DUPLICATE)
        self.assertEqual(list(self.read.active), [session.session_id])

        finished = parse_training_event(envelope(session, finished=True))
        self.assertEqual(self.project(finished), ProjectionOutcome.PROJECTED)
        self.assertEqual(self.project(finished), ProjectionOutcome.DUPLICATE)

        self.assertEqual(self.read.active, {})
        self.assertEqual(list(self.read.sessions), [session.session_id])
        self.assertEqual(self.read.sessions[session.session_id].version, 2)

    def test_finish_before_start_never_leaves_an_active_session(self) -> None:
        session = a_session()

        self.project(parse_training_event(envelope(session, finished=True)))
        late_start = self.project(parse_training_event(envelope(session)))

        self.assertEqual(late_start, ProjectionOutcome.STALE)
        self.assertEqual(self.read.active, {})
        self.assertEqual(len(self.read.sessions), 1)

    def test_republished_events_with_new_ids_do_not_duplicate(self) -> None:
        session = a_session()
        for finished in (False, False, True, True):
            self.project(parse_training_event(envelope(session, finished=finished)))

        self.assertEqual((self.read.active, len(self.read.sessions)), ({}, 1))
        self.assertEqual(len(self.read.processed), 4)

    def test_started_contract_violations(self) -> None:
        session = a_session()
        payload = session_started_payload(session)
        cases: dict[str, Any] = {
            "aggregate_id_mismatch": envelope(session, aggregate_id=str(uuid4())),
            "unknown_status": envelope(session, payload={**payload, "status": "FINISHED"}),
            "unknown_sport": envelope(session, payload={**payload, "sport": "golf"}),
            "started_at_not_datetime": envelope(session, payload={**payload, "started_at": "x"}),
            "athlete_id_not_uuid": envelope(session, payload={**payload, "athlete_id": "x"}),
        }
        for reason, body in cases.items():
            with self.subTest(reason=reason), self.assertRaises(InvalidEvent) as raised:
                parse_training_event(body)
            self.assertEqual(raised.exception.reason, reason)

    def test_handler_acks_after_projecting(self) -> None:
        message = Message(envelope(a_session()))

        ProjectionMessageHandler(self.project)(message)

        self.assertTrue(message.acked)
        self.assertEqual(len(self.read.active), 1)


if __name__ == "__main__":
    unittest.main()
