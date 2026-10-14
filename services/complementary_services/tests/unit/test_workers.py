"""`servicios-outbox-publisher` de HU019 sin infraestructura (ADR-027): sobre, orden,
reintentos de 10 a 600 s, alerta desde el décimo fallo y purga de publicados."""

import logging
import os
import threading
import unittest
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from unittest import mock

from fakes import NOW, FixedClock

from complementary_services.infrastructure.persistence.outbox import ClaimedEvent
from complementary_services.settings import (
    PUBLISHED_RETENTION,
    SERVICES_TOPIC,
    OutboxPublisherSettings,
)
from complementary_services.workers import outbox_publisher
from complementary_services.workers.outbox_publisher import (
    OutboxRelay,
    build_envelope,
    retry_delay_seconds,
)
from complementary_services.workers.pubsub_setup import ensure_resources
from sportapp_platform.config import ConfigurationError


def claimed(attempts: int = 0) -> ClaimedEvent:
    service_id = uuid.uuid4()
    return ClaimedEvent(
        event_id=uuid.uuid4(),
        event_type="servicio.publicado.v1",
        schema_version=1,
        aggregate_type="service",
        aggregate_id=service_id,
        aggregate_version=2,
        occurred_at=NOW + timedelta(hours=2),
        correlation_id=uuid.uuid4(),
        causation_id=str(uuid.uuid4()),
        payload={
            "service_id": str(service_id),
            "provider_id": str(uuid.uuid4()),
            "status": "PUBLISHED",
            "published_at": "2026-10-12T17:00:00Z",
        },
        attempts=attempts,
    )


def settings(**overrides: Any) -> OutboxPublisherSettings:
    values: dict[str, Any] = {
        "app_env": "test",
        "database_url": "postgresql+psycopg://contract@127.0.0.1:1/contract",
        "pubsub_project_id": "p",
        "worker_id": "w-1",
    }
    values.update(overrides)
    return OutboxPublisherSettings(**values)


@dataclass
class FakeOutboxStore:
    events: list[ClaimedEvent] = field(default_factory=list)
    published: list[uuid.UUID] = field(default_factory=list)
    failed: list[tuple[uuid.UUID, str, float]] = field(default_factory=list)
    purges: list[datetime] = field(default_factory=list)
    purge_error: Exception | None = None

    def claim_batch(self, worker_id: str, batch_size: int, lease_seconds: float) -> list[Any]:
        claimed, self.events = self.events[:batch_size], self.events[batch_size:]
        return claimed

    def mark_published(self, event_id: uuid.UUID, worker_id: str) -> None:
        self.published.append(event_id)

    def mark_failed(
        self, event_id: uuid.UUID, worker_id: str, error: str, retry_in_seconds: float
    ) -> None:
        self.failed.append((event_id, error, retry_in_seconds))

    def purge_published(self, older_than: datetime) -> int:
        self.purges.append(older_than)
        if self.purge_error is not None:
            raise self.purge_error
        return 3


class RecordingPublisher:
    def __init__(self) -> None:
        self.sent: list[tuple[dict[str, Any], str]] = []

    def publish(self, envelope: dict[str, Any], ordering_key: str) -> None:
        self.sent.append((envelope, ordering_key))


class FailingPublisher:
    def publish(self, envelope: dict[str, Any], ordering_key: str) -> None:
        raise RuntimeError("sin ACK")


class SettingsTest(unittest.TestCase):
    def test_publishes_to_the_services_topic_by_default(self) -> None:
        self.assertEqual(settings().pubsub_topic, SERVICES_TOPIC)
        self.assertEqual(SERVICES_TOPIC, "sportapp.servicios.v1")
        self.assertEqual(PUBLISHED_RETENTION, timedelta(days=7))


class OutboxRelayTest(unittest.TestCase):
    def relay(self, store: FakeOutboxStore, publisher: Any, **overrides: Any) -> OutboxRelay:
        self.clock = FixedClock()
        return OutboxRelay(store, publisher, settings(**overrides), clock=self.clock)

    def test_envelope_and_ordering_key(self) -> None:
        """Sobre de ADR-027 con `producer` del dominio y `service_id` como clave de orden;
        se marca publicado solo tras el ACK."""
        event = claimed()
        publisher = RecordingPublisher()
        store = FakeOutboxStore([event])

        self.assertEqual(self.relay(store, publisher).run_once(), 1)

        self.assertEqual(store.published, [event.event_id])
        [(body, key)] = publisher.sent
        self.assertEqual(key, str(event.aggregate_id))
        self.assertEqual(body, build_envelope(event))
        self.assertEqual(body["producer"], "complementary-services")
        self.assertEqual(body["aggregate_type"], "service")
        self.assertEqual(body["occurred_at"], "2026-10-12T17:00:00Z")
        self.assertEqual(body["causation_id"], event.causation_id)
        self.assertEqual(body["correlation_id"], str(event.correlation_id))

    def test_envelope_without_correlation(self) -> None:
        event = ClaimedEvent(**{**claimed().__dict__, "correlation_id": None})

        self.assertIsNone(build_envelope(event)["correlation_id"])

    def test_failure_keeps_event_pending_and_reschedules(self) -> None:
        store = FakeOutboxStore([claimed()])

        with self.assertLogs("sportapp.outbox", level="WARNING") as logs:
            self.relay(store, FailingPublisher()).run_once()

        self.assertEqual(store.published, [])
        [(_, error, delay)] = store.failed
        self.assertEqual(error, "RuntimeError")
        self.assertGreaterEqual(delay, 10)
        self.assertLessEqual(delay, 11)
        [record] = logs.records
        self.assertEqual((record.getMessage(), record.levelno), ("outbox.publish_failed", 30))

    def test_alert_from_the_tenth_failed_attempt(self) -> None:
        """ADR-027: alerta (`ERROR`) desde el décimo fallo, con `event_id` y `attempts`."""
        ninth, tenth, later = claimed(attempts=8), claimed(attempts=9), claimed(attempts=40)
        store = FakeOutboxStore([ninth, tenth, later])

        with self.assertLogs("sportapp.outbox", level="WARNING") as logs:
            self.relay(store, FailingPublisher()).run_once()

        messages = [(r.getMessage(), r.levelno) for r in logs.records]
        self.assertEqual(
            messages,
            [
                ("outbox.publish_failed", logging.WARNING),
                ("outbox.publish_alert", logging.ERROR),
                ("outbox.publish_alert", logging.ERROR),
            ],
        )
        alert = logs.records[1].fields  # type: ignore[attr-defined]
        self.assertEqual((alert["event_id"], alert["attempts"]), (str(tenth.event_id), 10))
        self.assertEqual(logs.records[2].fields["attempts"], 41)  # type: ignore[attr-defined]
        self.assertEqual(len(store.failed), 3, "un pendiente nunca se descarta")
        self.assertTrue(all(delay <= 600 for *_, delay in store.failed))

    def test_logs_do_not_carry_the_payload(self) -> None:
        event = claimed(attempts=9)

        with self.assertLogs("sportapp.outbox", level="WARNING") as logs:
            self.relay(FakeOutboxStore([event]), FailingPublisher()).run_once()

        self.assertNotIn(event.payload["provider_id"], str(logs.records[0].fields))  # type: ignore[attr-defined]

    def test_published_events_are_purged_at_most_once_per_hour(self) -> None:
        """ADR-027: se conservan siete días; la purga no corre en cada ciclo."""
        store = FakeOutboxStore()
        relay = self.relay(store, RecordingPublisher())

        relay.run_once()
        self.clock.advance(timedelta(minutes=59))
        relay.run_once()
        self.clock.advance(timedelta(minutes=1))
        relay.run_once()

        self.assertEqual(
            store.purges,
            [NOW - timedelta(days=7), NOW + timedelta(hours=1) - timedelta(days=7)],
        )

    def test_purge_failure_does_not_stop_publishing(self) -> None:
        event = claimed()
        store = FakeOutboxStore([event], purge_error=RuntimeError("base caída"))
        relay = self.relay(store, RecordingPublisher())

        with self.assertLogs("sportapp.outbox", level="WARNING") as logs:
            relay.run_once()
        relay.run_once()

        self.assertEqual(logs.records[0].getMessage(), "outbox.purge_failed")
        self.assertEqual(store.published, [event.event_id])
        self.assertEqual(len(store.purges), 1, "no se reintenta antes de una hora")

    def test_run_forever_drains_backlog_and_stops(self) -> None:
        """Recuperación básica: un backlog mayor que el lote se publica en varios ciclos."""
        backlog = [claimed() for _ in range(5)]
        store = FakeOutboxStore(list(backlog))
        stop = threading.Event()

        class Publisher:
            def publish(self, body: dict[str, Any], ordering_key: str) -> None:
                if len(store.published) == len(backlog) - 1:
                    stop.set()

        self.relay(store, Publisher(), outbox_batch_size=2).run_forever(stop)

        self.assertEqual(store.published, [event.event_id for event in backlog])

    def test_run_forever_survives_a_failed_cycle(self) -> None:
        stop = threading.Event()

        class BrokenStore(FakeOutboxStore):
            def claim_batch(
                self, worker_id: str, batch_size: int, lease_seconds: float
            ) -> list[Any]:
                stop.set()
                raise RuntimeError("base caída")

        with self.assertLogs("sportapp.outbox", level="ERROR") as logs:
            self.relay(BrokenStore(), RecordingPublisher()).run_forever(stop)

        self.assertEqual(logs.records[0].getMessage(), "outbox.cycle_failed")


class RetryDelayTest(unittest.TestCase):
    def test_progressive_wait_from_10_to_600_seconds(self) -> None:
        with mock.patch.object(outbox_publisher.random, "uniform", return_value=0.0):
            delays = [retry_delay_seconds(attempt) for attempt in range(1, 9)]

        self.assertEqual(delays, [10, 20, 40, 80, 160, 320, 600, 600])

    def test_jitter_never_leaves_the_approved_range(self) -> None:
        for jitter in (-0.1, 0.1):
            with mock.patch.object(outbox_publisher.random, "uniform", return_value=jitter):
                for attempt in (0, 1, 2, 7, 10, 1000):
                    with self.subTest(jitter=jitter, attempt=attempt):
                        self.assertGreaterEqual(retry_delay_seconds(attempt), 10)
                        self.assertLessEqual(retry_delay_seconds(attempt), 600)

    def test_jitter_spreads_retries(self) -> None:
        with mock.patch.object(outbox_publisher.random, "uniform", return_value=0.1):
            self.assertAlmostEqual(retry_delay_seconds(3), 44.0)


class PubSubSetupTest(unittest.TestCase):
    def test_refuses_to_run_outside_the_emulator(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(ConfigurationError):
            ensure_resources("sportapp-local")


if __name__ == "__main__":
    unittest.main()
