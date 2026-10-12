"""Workers y adaptadores de HU010 sin infraestructura: mensajes, proyector, outbox,
capacidades de Afiliación y política de permisos."""

import json
import os
import unittest
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from unittest import mock

import httpx
from fakes import NOW, FixedClock, InMemoryProjectionUnitOfWork, InMemoryReadStore

from sportapp_platform.authorization import load_actor_permissions, resolve_actor_permissions_path
from sportapp_platform.config import ConfigurationError
from training_management.application.errors import CapabilitiesUnavailable
from training_management.application.messages import (
    InvalidEvent,
    UnsupportedEvent,
    parse_training_event,
)
from training_management.application.projections.plan_projection import ProjectPlanCreated
from training_management.domain.plans.events import plan_created_payload
from training_management.domain.plans.model import PlannedSession, Sport, TrainingPlan
from training_management.infrastructure.authorization import ContractPermissionPolicy
from training_management.infrastructure.integrations.affiliation import (
    AffiliationCapabilityChecker,
)
from training_management.infrastructure.persistence_write.outbox import ClaimedEvent
from training_management.settings import OutboxPublisherSettings
from training_management.workers.outbox_publisher import OutboxRelay, build_envelope
from training_management.workers.pubsub_setup import ensure_resources
from training_management.workers.read_projector import ProjectionMessageHandler


def a_plan() -> TrainingPlan:
    return TrainingPlan.create(
        plan_id=uuid.uuid4(),
        athlete_id=uuid.uuid4(),
        professional_id=uuid.uuid4(),
        title="Base aeróbica",
        objectives=["Correr 10 km"],
        start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 29),
        sessions=[PlannedSession(date(2026, 11, 2), Sport.RUNNING, 40, 5)],
        now=NOW,
    )


def envelope(plan: TrainingPlan | None = None, **overrides: Any) -> dict[str, Any]:
    plan = plan or a_plan()
    values: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "event_type": "plan.entrenamiento_creado.v1",
        "occurred_at": NOW.isoformat(),
        "producer": "training-management",
        "aggregate_type": "training_plan",
        "aggregate_id": str(plan.plan_id),
        "aggregate_version": 1,
        "correlation_id": str(uuid.uuid4()),
        "causation_id": str(uuid.uuid4()),
        "schema_version": 1,
        "payload": plan_created_payload(plan),
    }
    values.update(overrides)
    return values


@dataclass
class Message:
    body: dict[str, Any] | bytes
    message_id: str = "m-1"
    delivery_attempt: int | None = 1
    acked: bool = False
    nacked: bool = False

    @property
    def data(self) -> bytes:
        return self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()

    def ack(self) -> None:
        self.acked = True

    def nack(self) -> None:
        self.nacked = True


class ParseTrainingEventTest(unittest.TestCase):
    def test_valid_event(self) -> None:
        plan = a_plan()
        event = parse_training_event(envelope(plan))

        self.assertEqual(event.plan.plan_id, plan.plan_id)
        self.assertEqual(event.plan.sessions[0].sport, "running")
        self.assertEqual(event.aggregate_version, 1)

    def test_other_event_types_are_unsupported(self) -> None:
        with self.assertRaises(UnsupportedEvent):
            parse_training_event(envelope(event_type="entrenamiento.iniciado.v1"))

    def test_contract_violations(self) -> None:
        plan = a_plan()
        payload = plan_created_payload(plan)
        cases: dict[str, Any] = {
            "envelope_not_object": [],
            "event_type_missing": envelope(event_type=None),
            "unsupported_schema_version": envelope(schema_version=2),
            "payload_not_object": envelope(payload="x"),
            "aggregate_version_invalid": envelope(aggregate_version=0),
            "aggregate_id_mismatch": envelope(plan, aggregate_id=str(uuid.uuid4())),
            "plan_version_mismatch": envelope(plan, payload={**payload, "plan_version": 2}),
            "event_id_not_uuid": envelope(event_id="x"),
            "unknown_status": envelope(plan, payload={**payload, "status": "DRAFT"}),
            "title_invalid": envelope(plan, payload={**payload, "title": ""}),
            "objectives_invalid": envelope(plan, payload={**payload, "objectives": [1]}),
            "sessions_invalid": envelope(plan, payload={**payload, "sessions": []}),
            "session_not_object": envelope(plan, payload={**payload, "sessions": ["x"]}),
            "unknown_sport": envelope(
                plan, payload={**payload, "sessions": [{**payload["sessions"][0], "sport": "golf"}]}
            ),
            "intensity_rpe_out_of_range": envelope(
                plan,
                payload={**payload, "sessions": [{**payload["sessions"][0], "intensity_rpe": 11}]},
            ),
            "scheduled_date_not_date": envelope(
                plan,
                payload={
                    **payload,
                    "sessions": [{**payload["sessions"][0], "scheduled_date": "x"}],
                },
            ),
            "created_at_not_datetime": envelope(plan, payload={**payload, "created_at": "x"}),
        }
        for reason, body in cases.items():
            with self.subTest(reason=reason), self.assertRaises(InvalidEvent) as raised:
                parse_training_event(body)
            self.assertEqual(raised.exception.reason, reason)


class ProjectionMessageHandlerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.read = InMemoryReadStore()
        self.handler = ProjectionMessageHandler(
            ProjectPlanCreated(
                unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.read), clock=FixedClock()
            )
        )

    def test_acks_after_projecting_and_on_duplicate(self) -> None:
        body = envelope()
        first, second = Message(body), Message(body)

        self.handler(first)
        self.handler(second)

        self.assertTrue(first.acked and second.acked)
        self.assertEqual(len(self.read.views), 1)

    def test_invalid_messages_are_not_acked(self) -> None:
        for body in (b"no-json", envelope(schema_version=9)):
            message = Message(body)
            with self.subTest(body=str(body)[:20]):
                with self.assertLogs("sportapp.projector", level="WARNING"):
                    self.handler(message)
                self.assertEqual((message.acked, message.nacked), (False, True))

    def test_unsupported_types_are_acked_without_effects(self) -> None:
        message = Message(envelope(event_type="indicador.calculado.v1"))

        self.handler(message)

        self.assertTrue(message.acked)
        self.assertEqual(self.read.views, {})

    def test_version_gap_or_failure_is_not_acked(self) -> None:
        plan = a_plan()
        payload = {**plan_created_payload(plan), "plan_version": 2}
        gap = Message(envelope(plan, aggregate_version=2, payload=payload))

        with self.assertLogs("sportapp.projector", level="WARNING"):
            self.handler(gap)

        self.assertEqual((gap.acked, gap.nacked), (False, True))


@dataclass
class FakeOutboxStore:
    events: list[ClaimedEvent]
    published: list[uuid.UUID] = field(default_factory=list)
    failed: list[tuple[uuid.UUID, str]] = field(default_factory=list)

    def claim_batch(self, worker_id: str, batch_size: int, lease_seconds: float) -> list[Any]:
        return list(self.events)

    def mark_published(self, event_id: uuid.UUID, worker_id: str) -> None:
        self.published.append(event_id)

    def mark_failed(
        self, event_id: uuid.UUID, worker_id: str, error: str, retry_in_seconds: float
    ) -> None:
        self.failed.append((event_id, error))


class OutboxRelayTest(unittest.TestCase):
    def claimed(self) -> ClaimedEvent:
        plan = a_plan()
        return ClaimedEvent(
            event_id=uuid.uuid4(),
            event_type="plan.entrenamiento_creado.v1",
            schema_version=1,
            aggregate_type="training_plan",
            aggregate_id=plan.plan_id,
            aggregate_version=1,
            occurred_at=datetime(2026, 10, 12, 7, 0, tzinfo=UTC),
            correlation_id=None,
            causation_id="k-1",
            payload=plan_created_payload(plan),
            attempts=0,
        )

    def settings(self) -> OutboxPublisherSettings:
        return OutboxPublisherSettings(
            app_env="test",
            database_write_url="postgresql+psycopg://contract@127.0.0.1:1/contract",  # type: ignore[arg-type]
            pubsub_project_id="p",
            worker_id="w-1",
        )

    def test_envelope_and_ordering_key(self) -> None:
        event = self.claimed()
        sent: list[tuple[dict[str, Any], str]] = []

        class Publisher:
            def publish(self, body: dict[str, Any], ordering_key: str) -> None:
                sent.append((body, ordering_key))

        store = FakeOutboxStore([event])
        published = OutboxRelay(store, Publisher(), self.settings()).run_once()

        self.assertEqual(published, 1)
        self.assertEqual(store.published, [event.event_id])
        body, key = sent[0]
        self.assertEqual(key, str(event.aggregate_id))
        self.assertEqual(body, build_envelope(event))
        self.assertEqual(body["producer"], "training-management")
        self.assertEqual(body["aggregate_type"], "training_plan")

    def test_failure_keeps_event_pending(self) -> None:
        class Failing:
            def publish(self, body: dict[str, Any], ordering_key: str) -> None:
                raise RuntimeError("sin ACK")

        store = FakeOutboxStore([self.claimed()])
        with self.assertLogs("sportapp.outbox", level="WARNING"):
            OutboxRelay(store, Failing(), self.settings()).run_once()

        self.assertEqual(store.published, [])
        self.assertEqual(store.failed[0][1], "RuntimeError")


class AffiliationCapabilityCheckerTest(unittest.TestCase):
    def checker(self, handler: Any) -> AffiliationCapabilityChecker:
        return AffiliationCapabilityChecker(
            httpx.Client(transport=httpx.MockTransport(handler), base_url="http://affiliation")
        )

    def test_active_snapshot_with_capability(self) -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return httpx.Response(
                200,
                json={"subscription_status": "ACTIVE", "capabilities": ["training.plan.manage"]},
            )

        user = uuid.uuid4()
        checker = self.checker(handler)

        self.assertTrue(checker.has_capability(user, "training.plan.manage"))
        self.assertFalse(checker.has_capability(user, "training.realtime.monitor"))
        self.assertEqual(paths[0], f"/v1/subscriptions/by-user/{user}/capabilities")

    def test_none_status_grants_nothing(self) -> None:
        checker = self.checker(
            lambda _: httpx.Response(
                200, json={"subscription_status": "NONE", "capabilities": ["training.plan.manage"]}
            )
        )

        self.assertFalse(checker.has_capability(uuid.uuid4(), "training.plan.manage"))

    def test_failures_are_unavailability(self) -> None:
        def down(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("caída")

        for handler in (
            down,
            lambda _: httpx.Response(500),
            lambda _: httpx.Response(200, content=b"no-json"),
            lambda _: httpx.Response(200, json=[]),
        ):
            with self.subTest(), self.assertRaises(CapabilitiesUnavailable):
                self.checker(handler).has_capability(uuid.uuid4(), "training.plan.manage")


class ContractPermissionPolicyTest(unittest.TestCase):
    def test_reads_versioned_contract(self) -> None:
        policy = ContractPermissionPolicy(load_actor_permissions(resolve_actor_permissions_path()))

        self.assertTrue(policy.allows("service_provider", "training.plan.author"))
        self.assertFalse(policy.allows("organizer", "training.plan.author"))
        self.assertFalse(policy.allows("athlete", "training.plan.author"))
        self.assertFalse(policy.allows("desconocido", "training.plan.author"))


class PubSubSetupTest(unittest.TestCase):
    def test_refuses_to_run_outside_the_emulator(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(ConfigurationError):
            ensure_resources("sportapp-local")


if __name__ == "__main__":
    unittest.main()
