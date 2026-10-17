import threading
import unittest
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from users_management.infrastructure.persistence_write.outbox import ClaimedEvent
from users_management.settings import OutboxPublisherSettings
from users_management.workers.outbox_publisher import (
    MAX_RETRY_SECONDS,
    OutboxRelay,
    build_envelope,
    retry_delay_seconds,
)

SETTINGS = OutboxPublisherSettings(
    app_env="test",  # type: ignore[arg-type]
    database_write_url="postgresql+psycopg://x@127.0.0.1:1/x",  # type: ignore[arg-type]
    pubsub_project_id="sportapp-test",
    worker_id="worker-1",
    outbox_batch_size=2,
)


def claimed(attempts: int = 0) -> ClaimedEvent:
    return ClaimedEvent(
        event_id=uuid4(),
        event_type="usuario.registrado.v1",
        schema_version=1,
        aggregate_type="user_account",
        aggregate_id=uuid4(),
        aggregate_version=1,
        occurred_at=datetime(2026, 10, 7, 12, 0, tzinfo=UTC),
        correlation_id=uuid4(),
        causation_id=str(uuid4()),
        payload={"user_id": "x"},
        attempts=attempts,
    )


class FakeStore:
    def __init__(self, batches: list[list[ClaimedEvent]]) -> None:
        self.batches = batches
        self.published: list[UUID] = []
        self.failed: list[tuple[UUID, str, float]] = []

    def claim_batch(
        self, worker_id: str, batch_size: int, lease_seconds: float
    ) -> list[ClaimedEvent]:
        return self.batches.pop(0) if self.batches else []

    def mark_published(self, event_id: UUID, worker_id: str) -> None:
        self.published.append(event_id)

    def mark_failed(
        self, event_id: UUID, worker_id: str, error: str, retry_in_seconds: float
    ) -> None:
        self.failed.append((event_id, error, retry_in_seconds))


class FakePublisher:
    def __init__(self, fail_for: set[UUID] | None = None) -> None:
        self.fail_for = fail_for or set()
        self.sent: list[tuple[dict[str, Any], str]] = []
        self.aggregate_types: list[str] = []

    def publish(self, envelope: dict[str, Any], ordering_key: str, aggregate_type: str) -> None:
        if UUID(envelope["event_id"]) in self.fail_for:
            raise TimeoutError("pubsub no respondió")
        self.sent.append((envelope, ordering_key))
        self.aggregate_types.append(aggregate_type)


class EnvelopeTest(unittest.TestCase):
    def test_envelope_contains_minimum_fields(self) -> None:
        event = claimed()

        envelope = build_envelope(event)

        self.assertEqual(
            set(envelope),
            {
                "event_id",
                "event_type",
                "occurred_at",
                "producer",
                "aggregate_id",
                "aggregate_version",
                "correlation_id",
                "causation_id",
                "schema_version",
                "payload",
            },
        )
        self.assertEqual(envelope["producer"], "gestion-usuarios")
        self.assertEqual(envelope["occurred_at"], "2026-10-07T12:00:00+00:00")

    def test_retry_delay_grows_and_is_capped(self) -> None:
        self.assertLess(retry_delay_seconds(1), 3.0)
        self.assertGreaterEqual(retry_delay_seconds(3), 8.0)
        self.assertLessEqual(retry_delay_seconds(50), MAX_RETRY_SECONDS + 1)


class OutboxRelayTest(unittest.TestCase):
    def test_publishes_with_aggregate_ordering_key_then_marks_published(self) -> None:
        event = claimed()
        store, publisher = FakeStore([[event]]), FakePublisher()

        processed = OutboxRelay(store, publisher, SETTINGS).run_once()

        [(envelope, ordering_key)] = publisher.sent
        self.assertEqual(processed, 1)
        self.assertEqual(ordering_key, str(event.aggregate_id))
        self.assertEqual(envelope["event_id"], str(event.event_id))
        self.assertEqual(publisher.aggregate_types, ["user_account"])
        self.assertEqual(store.published, [event.event_id])

    def test_failed_publication_is_rescheduled_and_not_marked(self) -> None:
        ok, failing = claimed(), claimed(attempts=2)
        store = FakeStore([[failing, ok]])
        publisher = FakePublisher(fail_for={failing.event_id})

        with self.assertLogs("sportapp.outbox", level="WARNING"):
            OutboxRelay(store, publisher, SETTINGS).run_once()

        self.assertEqual(store.published, [ok.event_id])
        [(event_id, error, delay)] = store.failed
        self.assertEqual(event_id, failing.event_id)
        self.assertEqual(error, "TimeoutError")
        self.assertGreaterEqual(delay, 8.0)

    def test_run_forever_stops_when_requested(self) -> None:
        store = FakeStore([[claimed(), claimed()], [claimed()]])
        stop = threading.Event()
        relay = OutboxRelay(store, FakePublisher(), SETTINGS)
        thread = threading.Thread(target=relay.run_forever, args=(stop,))

        thread.start()
        while store.batches:
            stop.wait(0.01)
        stop.set()
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(store.published), 3)


if __name__ == "__main__":
    unittest.main()
