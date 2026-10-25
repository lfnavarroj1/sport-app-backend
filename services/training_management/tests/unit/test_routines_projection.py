"""HU030 (ADR-041): carrera de la clave de idempotencia, lectura validada de
`plantilla_rutina.*.v1` y su proyección en `nutrition_routine_template_view` (duplicados, orden
inverso, reconstrucción y rechazo hacia la DLQ)."""

import json
import unittest
from datetime import timedelta
from typing import Any
from uuid import uuid4

from fakes import FakePermissions, FixedClock, InMemoryProjectionUnitOfWork, InMemoryReadStore
from nutrition_fakes import StaticFoodCatalog
from routine_fakes import (
    AUTHOR,
    InMemoryRoutineStore,
    InMemoryRoutineUnitOfWork,
    routine_input,
)

from training_management.application.commands.nutrition_routine_templates import (
    PublishRoutine,
    PublishRoutineCommand,
    RetireRoutine,
    RetireRoutineCommand,
)
from training_management.application.messages import (
    InvalidEvent,
    RoutineChangedEvent,
    parse_training_event,
)
from training_management.application.ports import Caller
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.application.projections.snapshot_projection import TrainingProjector
from training_management.domain.routines.model import RoutineStatus
from training_management.infrastructure.persistence_write.outbox import ClaimedEvent
from training_management.workers.outbox_publisher import build_envelope
from training_management.workers.read_projector import ProjectionMessageHandler


class _Message:
    def __init__(self, envelope: dict[str, Any]) -> None:
        self.data = json.dumps(envelope).encode()
        self.message_id = "m-1"
        self.delivery_attempt = 1
        self.acked = self.nacked = False

    def ack(self) -> None:
        self.acked = True

    def nack(self) -> None:
        self.nacked = True


class RoutineFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryRoutineStore()
        self.clock = FixedClock()
        self.professional = Caller(uuid4(), "service_provider")
        self.hook: Any = None
        self.calls, self.hook_on_call = 0, 0
        self.permissions = FakePermissions({"service_provider": {AUTHOR}})
        self.catalog = StaticFoodCatalog()
        self.publish = PublishRoutine(
            unit_of_work=self.unit_of_work,
            permissions=self.permissions,
            catalog=self.catalog,
            clock=self.clock,
        )
        self.retire = RetireRoutine(
            unit_of_work=self.unit_of_work, permissions=self.permissions, clock=self.clock
        )
        self.read = InMemoryReadStore()
        self.project = TrainingProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.read), clock=self.clock
        )

    def unit_of_work(self) -> InMemoryRoutineUnitOfWork:
        self.calls += 1
        hook = self.hook if self.calls == self.hook_on_call else None
        return InMemoryRoutineUnitOfWork(self.store, before_commit=hook)

    def envelopes(self) -> list[dict[str, Any]]:
        return [
            json.loads(
                json.dumps(
                    build_envelope(
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
                )
            )
            for message in self.store.outbox
        ]

    def publish_and_retire(self) -> None:
        routine = self.publish(
            self.professional, PublishRoutineCommand(routine_input(), uuid4())
        ).routine
        self.clock.advance(timedelta(hours=1))
        self.retire(self.professional, RetireRoutineCommand(routine.template_id, 1))

    def test_concurrent_request_with_the_same_key_returns_the_winner(self) -> None:
        command = PublishRoutineCommand(routine_input(), uuid4())
        rival = PublishRoutine(
            unit_of_work=lambda: InMemoryRoutineUnitOfWork(self.store),
            permissions=self.permissions,
            catalog=self.catalog,
            clock=self.clock,
        )
        winner: list[Any] = []
        # La primera unidad solo busca la clave; la segunda confirma y choca con la rival.
        self.hook_on_call = 2
        self.hook = lambda: winner.append(rival(self.professional, command))

        result = self.publish(self.professional, command)

        self.assertTrue(result.replayed)
        self.assertEqual(result.routine.template_id, winner[0].routine.template_id)
        self.assertEqual((len(self.store.routines), len(self.store.outbox)), (1, 1))

    def test_events_project_full_state_and_retire_first_wins(self) -> None:
        self.publish_and_retire()
        published, retired = (parse_training_event(item) for item in self.envelopes())
        assert isinstance(published, RoutineChangedEvent)
        assert isinstance(retired, RoutineChangedEvent)

        self.assertEqual(self.project(retired), ProjectionOutcome.PROJECTED)
        self.assertEqual(self.project(published), ProjectionOutcome.STALE)
        self.assertEqual(self.project(retired), ProjectionOutcome.DUPLICATE)
        view = self.read.routines[retired.routine.template_id]
        self.assertEqual((view.status, view), (RoutineStatus.RETIRED, retired.routine))
        rebuilt = InMemoryReadStore()
        project = TrainingProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(rebuilt), clock=self.clock
        )
        for event in (published, retired):
            project(event)
        self.assertEqual(rebuilt.routines, self.read.routines)

    def test_projector_acks_valid_messages(self) -> None:
        self.publish_and_retire()
        messages = [_Message(envelope) for envelope in self.envelopes()]

        for message in messages:
            ProjectionMessageHandler(self.project)(message)

        self.assertEqual([(m.acked, m.nacked) for m in messages], [(True, False)] * 2)
        [view] = self.read.routines.values()
        self.assertEqual(view.version, 2)

    def test_contract_violations_go_to_the_dlq(self) -> None:
        self.publish_and_retire()
        published, retired = self.envelopes()
        payload = published["payload"]
        cases = {
            "aggregate_id_mismatch": {**published, "aggregate_id": str(uuid4())},
            "routine_version_mismatch": {**published, "aggregate_version": 2},
            "status_mismatch": {**retired, "event_type": "plantilla_rutina.publicada.v1"},
            "unsupported_schema_version": {**published, "schema_version": 2},
            "dietary_pattern_invalid": {
                **published,
                "payload": {**payload, "dietary_pattern": "PALEO"},
            },
            "contains_allergens_invalid": {
                **published,
                "payload": {**payload, "contains_allergens": ["GLUTEN"]},
            },
        }
        for reason, envelope in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaises(InvalidEvent) as error:
                    parse_training_event(envelope)
                self.assertEqual(error.exception.reason, reason)
                message = _Message(envelope)
                with self.assertLogs("sportapp.projector", level="WARNING"):
                    ProjectionMessageHandler(self.project)(message)
                self.assertEqual((message.acked, message.nacked), (False, True))
        self.assertEqual(self.read.routines, {})


if __name__ == "__main__":
    unittest.main()
