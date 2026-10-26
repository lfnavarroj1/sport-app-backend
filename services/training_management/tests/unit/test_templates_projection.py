"""HU028 (ADR-041): carrera de la clave de idempotencia, lectura validada de
`plantilla_entrenamiento.*.v1` y su proyección en `training_plan_template_view` (duplicados,
orden inverso, reconstrucción y rechazo hacia la DLQ)."""

import json
import unittest
from datetime import timedelta
from typing import Any
from uuid import uuid4

from fakes import FakePermissions, FixedClock, InMemoryProjectionUnitOfWork, InMemoryReadStore
from template_fakes import (
    AUTHOR,
    InMemoryTemplateStore,
    InMemoryTemplateUnitOfWork,
    template_input,
)

from training_management.application.commands.training_plan_templates import (
    PublishTemplate,
    PublishTemplateCommand,
    RetireTemplate,
    RetireTemplateCommand,
)
from training_management.application.messages import (
    InvalidEvent,
    TemplateChangedEvent,
    parse_training_event,
)
from training_management.application.ports import Caller
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.application.projections.snapshot_projection import TrainingProjector
from training_management.domain.templates.model import TemplateStatus
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


class TemplateFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryTemplateStore()
        self.clock = FixedClock()
        self.professional = Caller(uuid4(), "service_provider")
        self.hook: Any = None
        self.calls, self.hook_on_call = 0, 0
        self.permissions = FakePermissions({"service_provider": {AUTHOR}})
        self.publish = PublishTemplate(
            unit_of_work=self.unit_of_work, permissions=self.permissions, clock=self.clock
        )
        self.retire = RetireTemplate(
            unit_of_work=self.unit_of_work, permissions=self.permissions, clock=self.clock
        )
        self.read = InMemoryReadStore()
        self.project = TrainingProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.read), clock=self.clock
        )

    def unit_of_work(self) -> InMemoryTemplateUnitOfWork:
        self.calls += 1
        hook = self.hook if self.calls == self.hook_on_call else None
        return InMemoryTemplateUnitOfWork(self.store, before_commit=hook)

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
        template = self.publish(
            self.professional, PublishTemplateCommand(template_input(), uuid4())
        ).template
        self.clock.advance(timedelta(hours=1))
        self.retire(self.professional, RetireTemplateCommand(template.template_id, 1))

    def test_concurrent_request_with_the_same_key_returns_the_winner(self) -> None:
        command = PublishTemplateCommand(template_input(), uuid4())
        rival = PublishTemplate(
            unit_of_work=lambda: InMemoryTemplateUnitOfWork(self.store),
            permissions=self.permissions,
            clock=self.clock,
        )
        winner: list[Any] = []
        # La primera unidad solo busca la clave; la segunda confirma y choca con la rival.
        self.hook_on_call = 2
        self.hook = lambda: winner.append(rival(self.professional, command))

        result = self.publish(self.professional, command)

        self.assertTrue(result.replayed)
        self.assertEqual(result.template.template_id, winner[0].template.template_id)
        self.assertEqual((len(self.store.templates), len(self.store.outbox)), (1, 1))

    def test_events_project_full_state_and_retire_first_wins(self) -> None:
        self.publish_and_retire()
        published, retired = (parse_training_event(item) for item in self.envelopes())
        assert isinstance(published, TemplateChangedEvent)
        assert isinstance(retired, TemplateChangedEvent)

        self.assertEqual(self.project(retired), ProjectionOutcome.PROJECTED)
        self.assertEqual(self.project(published), ProjectionOutcome.STALE)
        self.assertEqual(self.project(retired), ProjectionOutcome.DUPLICATE)
        view = self.read.templates[retired.template.template_id]
        self.assertEqual((view.status, view), (TemplateStatus.RETIRED, retired.template))
        rebuilt = InMemoryReadStore()
        project = TrainingProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(rebuilt), clock=self.clock
        )
        for event in (published, retired):
            project(event)
        self.assertEqual(rebuilt.templates, self.read.templates)

    def test_contract_violations_go_to_the_dlq(self) -> None:
        self.publish_and_retire()
        published, retired = self.envelopes()
        cases = {
            "aggregate_id_mismatch": {**published, "aggregate_id": str(uuid4())},
            "template_version_mismatch": {**published, "aggregate_version": 2},
            "status_mismatch": {**retired, "event_type": "plantilla_entrenamiento.publicada.v1"},
            "unsupported_schema_version": {**published, "schema_version": 2},
            "goal_invalid": {**published, "payload": {**published["payload"], "goal": "FUN"}},
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
        self.assertEqual(self.read.templates, {})


if __name__ == "__main__":
    unittest.main()
