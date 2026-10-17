"""Consumidor del alta gratuita (ACK/NACK) y sobre del publicador outbox."""

import json
import threading
import unittest
import uuid
from datetime import UTC, datetime
from typing import Any

from fakes import InMemoryAffiliationStore, provision_with, user_registered

from affiliation_management.application.commands.provision_free_subscription import (
    ProvisioningOutcome,
)
from affiliation_management.infrastructure.persistence.outbox import ClaimedEvent
from affiliation_management.settings import OutboxPublisherSettings
from affiliation_management.workers.free_plan_consumer import (
    FreePlanMessageHandler,
    await_subscription,
)
from affiliation_management.workers.outbox_publisher import (
    OutboxRelay,
    build_envelope,
    retry_delay_seconds,
)


class FakeMessage:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.message_id = "m-1"
        self.delivery_attempt = 1
        self.acked = False
        self.nacked = False

    def ack(self) -> None:
        self.acked = True

    def nack(self) -> None:
        self.nacked = True


class FreePlanMessageHandlerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryAffiliationStore()
        self.handler = FreePlanMessageHandler(provision_with(self.store))

    def test_acks_after_provisioning(self) -> None:
        message = FakeMessage(json.dumps(user_registered()).encode())

        self.handler(message)

        self.assertTrue(message.acked)
        self.assertFalse(message.nacked)
        self.assertEqual(len(self.store.subscriptions), 1)

    def test_duplicate_and_not_applicable_events_are_acked(self) -> None:
        envelope = user_registered()
        first = FakeMessage(json.dumps(envelope).encode())
        repeated = FakeMessage(json.dumps(envelope).encode())
        organizer = FakeMessage(json.dumps(user_registered(actor_type="organizer")).encode())

        for message in (first, repeated, organizer):
            self.handler(message)

        self.assertTrue(all(m.acked for m in (first, repeated, organizer)))
        self.assertEqual(len(self.store.subscriptions), 1)

    def test_other_event_types_of_the_topic_are_acked_without_effects(self) -> None:
        """R-19 (ADR-034): defensa adicional al filtro de la suscripción."""
        message = FakeMessage(
            json.dumps(user_registered(event_type="perfil.fisiologico_actualizado.v1")).encode()
        )

        with self.assertLogs("sportapp.consumer", level="INFO") as logs:
            self.handler(message)

        self.assertTrue(message.acked)
        self.assertFalse(message.nacked)
        self.assertEqual(self.store.subscriptions, {})
        self.assertEqual(logs.records[0].getMessage(), "consumer.ignored_event")

    def test_invalid_messages_are_not_acked_so_they_reach_the_dlq(self) -> None:
        for data in (b"{no-json", json.dumps(user_registered(schema_version=9)).encode()):
            with self.subTest(data=data[:12]):
                message = FakeMessage(data)

                with self.assertLogs("sportapp.consumer", level="WARNING"):
                    self.handler(message)

                self.assertTrue(message.nacked)
                self.assertFalse(message.acked)
        self.assertEqual(self.store.subscriptions, {})

    def test_processing_failure_is_not_acked(self) -> None:
        def broken(_: object) -> ProvisioningOutcome:
            raise RuntimeError("base caída")

        message = FakeMessage(json.dumps(user_registered()).encode())

        with self.assertLogs("sportapp.consumer", level="WARNING") as logs:
            FreePlanMessageHandler(broken)(message)

        self.assertTrue(message.nacked)
        self.assertEqual(logs.records[0].fields["error_type"], "RuntimeError")  # type: ignore[attr-defined]


def claimed(**overrides: Any) -> ClaimedEvent:
    values: dict[str, Any] = {
        "event_id": uuid.uuid4(),
        "event_type": "suscripcion.activada.v1",
        "schema_version": 1,
        "aggregate_type": "user_subscription",
        "aggregate_id": uuid.uuid4(),
        "aggregate_version": 1,
        "occurred_at": datetime(2026, 10, 11, 12, 0, tzinfo=UTC),
        "correlation_id": uuid.uuid4(),
        "causation_id": str(uuid.uuid4()),
        "payload": {"plan_code": "FREE"},
        "attempts": 0,
    }
    values.update(overrides)
    return ClaimedEvent(**values)


class OutboxPublisherTest(unittest.TestCase):
    def test_envelope_follows_adr024(self) -> None:
        event = claimed()

        envelope = build_envelope(event)

        self.assertEqual(
            set(envelope),
            {
                "event_id",
                "event_type",
                "occurred_at",
                "producer",
                "aggregate_type",
                "aggregate_id",
                "aggregate_version",
                "correlation_id",
                "causation_id",
                "schema_version",
                "payload",
            },
        )
        self.assertEqual(envelope["producer"], "affiliation-management")
        self.assertEqual(envelope["aggregate_type"], "user_subscription")
        self.assertIsNone(build_envelope(claimed(correlation_id=None))["correlation_id"])

    def test_relay_orders_by_user_and_reschedules_failures(self) -> None:
        good, bad = claimed(), claimed()
        store = FakeStore([good, bad])
        publisher = FakePublisher(fail_for=bad.event_id)
        settings = OutboxPublisherSettings(
            app_env="test",  # type: ignore[arg-type]
            database_url="postgresql+psycopg://u@127.0.0.1:1/a",  # type: ignore[arg-type]
            pubsub_project_id="p",
            worker_id="w-1",
        )

        with self.assertLogs("sportapp.outbox", level="INFO"):
            claimed_count = OutboxRelay(store, publisher, settings).run_once()

        self.assertEqual(claimed_count, 2)
        self.assertEqual(publisher.ordering_keys, [str(good.aggregate_id), str(bad.aggregate_id)])
        self.assertEqual(store.published, [good.event_id])
        self.assertEqual(store.failed[0][0], bad.event_id)
        self.assertGreaterEqual(retry_delay_seconds(20), 600)

    def test_run_forever_stops_when_requested(self) -> None:
        stop = threading.Event()
        stop.set()
        settings = OutboxPublisherSettings(
            app_env="test",  # type: ignore[arg-type]
            database_url="postgresql+psycopg://u@127.0.0.1:1/a",  # type: ignore[arg-type]
            pubsub_project_id="p",
        )

        OutboxRelay(FakeStore([]), FakePublisher(), settings).run_forever(stop)


class FakeStore:
    def __init__(self, batch: list[ClaimedEvent]) -> None:
        self.batch = batch
        self.published: list[uuid.UUID] = []
        self.failed: list[tuple[uuid.UUID, str]] = []

    def claim_batch(
        self, worker_id: str, batch_size: int, lease_seconds: float
    ) -> list[ClaimedEvent]:
        batch, self.batch = self.batch, []
        return batch

    def mark_published(self, event_id: uuid.UUID, worker_id: str) -> None:
        self.published.append(event_id)

    def mark_failed(
        self, event_id: uuid.UUID, worker_id: str, error: str, retry_in_seconds: float
    ) -> None:
        self.failed.append((event_id, error))


class FakePublisher:
    def __init__(self, fail_for: uuid.UUID | None = None) -> None:
        self.fail_for = fail_for
        self.ordering_keys: list[str] = []

    def publish(self, envelope: dict[str, Any], ordering_key: str) -> None:
        self.ordering_keys.append(ordering_key)
        if envelope["event_id"] == str(self.fail_for):
            raise TimeoutError("sin confirmación")


class _Streaming:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def result(self) -> object:
        if self.error is not None:
            raise self.error
        return None


class AwaitSubscriptionTest(unittest.TestCase):
    """HU010 (cierre E2E): un fin inesperado de la suscripción no se oculta como parada."""

    def test_signal_stop_exits_with_zero(self) -> None:
        stop = threading.Event()
        stop.set()

        with self.assertLogs("sportapp.consumer", level="INFO") as logs:
            code = await_subscription(_Streaming(RuntimeError("cancelada")), stop)

        self.assertEqual(code, 0)
        self.assertEqual([r.getMessage() for r in logs.records], ["consumer.stopping"])

    def test_subscription_failure_is_logged_as_error_and_exits_with_one(self) -> None:
        class NotFound(Exception):
            pass

        for streaming, error_type in (
            (_Streaming(NotFound("sin suscripción")), "NotFound"),
            (_Streaming(), None),
        ):
            with self.subTest(error_type=error_type):
                with self.assertLogs("sportapp.consumer", level="ERROR") as logs:
                    code = await_subscription(streaming, threading.Event())

                self.assertEqual(code, 1)
                [record] = logs.records
                self.assertEqual(record.getMessage(), "consumer.subscription_failed")
                self.assertEqual(record.fields["error_type"], error_type)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
