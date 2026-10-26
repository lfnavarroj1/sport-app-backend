"""Cargas asíncronas de HU031 sin infraestructura (ADR-042): bucle de `terceros-sync-worker`,
relevo de `terceros-outbox-publisher` (sobre, `ordering_key = user_id`, reintentos de 10 a
600 s, alerta desde el décimo fallo y purga), `pubsub_setup` solo contra el emulador,
configuración de Strava en `/health/ready` y variables exigidas al worker."""

import base64
import logging
import os
import secrets
import threading
import unittest
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any
from unittest import mock

from fakes import NOW, FixedClock
from pydantic import ValidationError

from sportapp_platform.config import ConfigurationError
from third_party_management.infrastructure.persistence.outbox import ClaimedEvent
from third_party_management.infrastructure.strava.configuration import (
    READINESS_CHECK,
    StravaConfigurationCheck,
    missing_strava_settings,
)
from third_party_management.settings import (
    PUBLISHED_RETENTION,
    TERCEROS_TOPIC,
    OutboxPublisherSettings,
    SyncWorkerSettings,
    ThirdPartyManagementSettings,
)
from third_party_management.workers import outbox_publisher
from third_party_management.workers.outbox_publisher import (
    OutboxRelay,
    build_envelope,
    ordering_key,
    retry_delay_seconds,
)
from third_party_management.workers.pubsub_setup import ensure_resources
from third_party_management.workers.sync_worker import SyncWorker

DATABASE_URL = "postgresql+psycopg://contract@127.0.0.1:1/contract"


def token_key(size: int = 32) -> str:
    return base64.urlsafe_b64encode(os.urandom(size)).decode("ascii").rstrip("=")


def claimed(attempts: int = 0) -> ClaimedEvent:
    activity_id, user_id = uuid.uuid4(), uuid.uuid4()
    return ClaimedEvent(
        event_id=uuid.uuid4(),
        event_type="actividad.importada.v1",
        schema_version=1,
        aggregate_type="external_activity",
        aggregate_id=activity_id,
        aggregate_version=1,
        occurred_at=NOW + timedelta(hours=2),
        correlation_id=uuid.uuid4(),
        causation_id=str(uuid.uuid4()),
        payload={
            "activity_id": str(activity_id),
            "user_id": str(user_id),
            "provider": "STRAVA",
            "external_activity_id": "9876543210",
            "name": "Ruta sintética",
        },
        attempts=attempts,
    )


def publisher_settings(**overrides: Any) -> OutboxPublisherSettings:
    values: dict[str, Any] = {
        "app_env": "test",
        "database_url": DATABASE_URL,
        "pubsub_project_id": "p",
        "worker_id": "w-1",
    }
    values.update(overrides)
    return OutboxPublisherSettings(**values)


def sync_settings(**overrides: Any) -> SyncWorkerSettings:
    values: dict[str, Any] = {
        "app_env": "test",
        "database_url": DATABASE_URL,
        "strava_client_id": "4242",
        "strava_client_secret": secrets.token_urlsafe(24),
        "strava_token_key": token_key(),
        "worker_id": "w-1",
    }
    values.update(overrides)
    return SyncWorkerSettings(**values)


def api_settings(**overrides: Any) -> ThirdPartyManagementSettings:
    values: dict[str, Any] = {
        "app_env": "test",
        "database_url": DATABASE_URL,
        "users_management_base_url": "http://users-management.invalid",
        "affiliation_management_base_url": "http://affiliation-management.invalid",
        "strava_client_id": "4242",
        "strava_client_secret": secrets.token_urlsafe(24),
        "strava_token_key": token_key(),
        "strava_redirect_uri": "https://bff-mobile.sportapp.test/v1/integrations/strava/callback",
        "strava_consent_version": "strava-consent-2026-10.v1",
    }
    values.update(overrides)
    return ThirdPartyManagementSettings(**values)


# ------------------------------------------------------------------ sync worker


class ScriptedRunSync:
    """Devuelve, ciclo a ciclo, una corrida procesada (`True`), ninguna (`False`) o lanza
    la excepción indicada; al agotar el guion detiene el bucle."""

    def __init__(self, stop: threading.Event, script: list[bool | Exception]) -> None:
        self.stop = stop
        self.script = list(script)
        self.calls = 0

    def __call__(self) -> object | None:
        self.calls += 1
        step = self.script.pop(0)
        if not self.script:
            self.stop.set()
        if isinstance(step, Exception):
            raise step
        return object() if step else None


class RecordingStop(threading.Event):
    """Registra cada espera del bucle sin dormir."""

    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout or 0.0)
        return self.is_set()


class SyncWorkerTest(unittest.TestCase):
    def worker(self, run_sync: ScriptedRunSync, **overrides: Any) -> SyncWorker:
        settings = sync_settings(
            sync_busy_poll_seconds=0.25, sync_idle_poll_max_seconds=2.0, **overrides
        )
        return SyncWorker(run_sync, settings)  # type: ignore[arg-type]

    def test_run_once_reports_whether_a_run_was_processed(self) -> None:
        stop = threading.Event()

        worker = self.worker(ScriptedRunSync(stop, [True, False, False]))

        self.assertTrue(worker.run_once())
        self.assertFalse(worker.run_once())

    def test_idle_wait_grows_up_to_the_maximum_and_resets_after_work(self) -> None:
        stop = RecordingStop()
        script: list[bool | Exception] = [False] * 6 + [True, False, False]
        run_sync = ScriptedRunSync(stop, script)

        self.worker(run_sync).run_forever(stop)

        self.assertEqual(run_sync.calls, len(script))
        self.assertEqual(stop.waits, [0.25, 0.5, 1.0, 2.0, 2.0, 2.0, 0.25, 0.25, 0.5])

    def test_a_failed_cycle_is_logged_and_the_loop_continues(self) -> None:
        stop = RecordingStop()
        run_sync = ScriptedRunSync(stop, [RuntimeError("base caída"), True, False])

        with self.assertLogs("sportapp.sync", level="ERROR") as logs:
            self.worker(run_sync).run_forever(stop)

        self.assertEqual(run_sync.calls, 3)
        self.assertEqual(logs.records[0].getMessage(), "sync.cycle_failed")
        self.assertEqual(stop.waits, [0.25, 0.25, 0.25])

    def test_stops_without_processing_when_already_stopped(self) -> None:
        stop = threading.Event()
        stop.set()
        run_sync = ScriptedRunSync(stop, [True])

        self.worker(run_sync).run_forever(stop)

        self.assertEqual(run_sync.calls, 0)


class SyncWorkerSettingsTest(unittest.TestCase):
    def test_requires_every_strava_credential(self) -> None:
        for name in ("strava_client_id", "strava_client_secret", "strava_token_key"):
            with self.subTest(missing=name):
                values = {
                    "app_env": "test",
                    "database_url": DATABASE_URL,
                    "strava_client_id": "4242",
                    "strava_client_secret": secrets.token_urlsafe(24),
                    "strava_token_key": token_key(),
                }
                del values[name]

                with self.assertRaises(ValidationError) as caught:
                    SyncWorkerSettings(**values)

                self.assertEqual([error["loc"] for error in caught.exception.errors()], [(name,)])

    def test_empty_client_id_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            sync_settings(strava_client_id="")

    def test_secrets_are_not_printed(self) -> None:
        settings = sync_settings()

        self.assertNotIn(settings.strava_client_secret.get_secret_value(), repr(settings))
        self.assertNotIn(settings.strava_token_key.get_secret_value(), repr(settings))

    def test_lease_covers_the_worst_case_run(self) -> None:
        self.assertEqual(sync_settings().sync_lease_seconds, 300)
        with self.assertRaises(ValidationError):
            sync_settings(sync_lease_seconds=30)


# ----------------------------------------------------------------- readiness


class StravaConfigurationCheckTest(unittest.TestCase):
    def test_passes_with_complete_configuration(self) -> None:
        check = StravaConfigurationCheck(api_settings())

        self.assertEqual(check.name, READINESS_CHECK)
        self.assertEqual(READINESS_CHECK, "strava_configuration")
        self.assertTrue(check())

    def test_fails_when_a_variable_is_missing(self) -> None:
        cases = {
            "STRAVA_CLIENT_ID": {"strava_client_id": None},
            "STRAVA_CLIENT_SECRET": {"strava_client_secret": None},
            "STRAVA_REDIRECT_URI": {"strava_redirect_uri": None},
            "STRAVA_CONSENT_VERSION": {"strava_consent_version": None},
            "STRAVA_TOKEN_KEY": {"strava_token_key": None},
        }
        for name, overrides in cases.items():
            with self.subTest(missing=name):
                settings = api_settings(**overrides)
                check = StravaConfigurationCheck(settings)

                with self.assertLogs("sportapp.strava", level="WARNING") as logs:
                    self.assertFalse(check())

                self.assertEqual(missing_strava_settings(settings), [name])
                [record] = logs.records
                self.assertEqual(record.getMessage(), "strava.configuration_incomplete")
                self.assertEqual(record.fields, {"missing": [name]})  # type: ignore[attr-defined]

    def test_fails_with_a_key_that_does_not_decode_to_32_bytes(self) -> None:
        for name, value in {
            "16 bytes": token_key(16),
            "33 bytes": token_key(33),
            "ilegible": "no es base64 ***",
            "vacía": "",
        }.items():
            with self.subTest(key=name):
                settings = api_settings(strava_token_key=value)

                with self.assertLogs("sportapp.strava", level="WARNING") as logs:
                    self.assertFalse(StravaConfigurationCheck(settings)())

                self.assertEqual(missing_strava_settings(settings), ["STRAVA_TOKEN_KEY"])
                self.assertNotIn(value or "\x00", str(logs.records[0].fields))  # type: ignore[attr-defined]

    def test_empty_secret_counts_as_missing(self) -> None:
        settings = api_settings(strava_client_secret="")

        self.assertEqual(missing_strava_settings(settings), ["STRAVA_CLIENT_SECRET"])

    def test_without_any_strava_variable_lists_them_all(self) -> None:
        settings = api_settings(
            strava_client_id=None,
            strava_client_secret=None,
            strava_token_key=None,
            strava_redirect_uri=None,
            strava_consent_version=None,
        )

        self.assertEqual(
            missing_strava_settings(settings),
            [
                "STRAVA_CLIENT_ID",
                "STRAVA_CLIENT_SECRET",
                "STRAVA_REDIRECT_URI",
                "STRAVA_CONSENT_VERSION",
                "STRAVA_TOKEN_KEY",
            ],
        )


# ------------------------------------------------------------- outbox relay


@dataclass
class FakeOutboxStore:
    events: list[ClaimedEvent] = field(default_factory=list)
    published: list[uuid.UUID] = field(default_factory=list)
    failed: list[tuple[uuid.UUID, str, float]] = field(default_factory=list)
    purges: list[datetime] = field(default_factory=list)
    purge_error: Exception | None = None

    def claim_batch(self, worker_id: str, batch_size: int, lease_seconds: float) -> list[Any]:
        batch, self.events = self.events[:batch_size], self.events[batch_size:]
        return batch

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


class OutboxSettingsTest(unittest.TestCase):
    def test_publishes_to_the_terceros_topic_by_default(self) -> None:
        self.assertEqual(publisher_settings().pubsub_topic, TERCEROS_TOPIC)
        self.assertEqual(TERCEROS_TOPIC, "sportapp.terceros.v1")
        self.assertEqual(PUBLISHED_RETENTION, timedelta(days=7))


class OutboxRelayTest(unittest.TestCase):
    def relay(self, store: FakeOutboxStore, publisher: Any, **overrides: Any) -> OutboxRelay:
        self.clock = FixedClock()
        return OutboxRelay(store, publisher, publisher_settings(**overrides), clock=self.clock)

    def test_envelope_and_ordering_key_by_user(self) -> None:
        """ADR-042 §4: `ordering_key = user_id` (distinta de `aggregate_id`); se marca
        publicado solo tras el ACK."""
        event = claimed()
        publisher = RecordingPublisher()
        store = FakeOutboxStore([event])

        self.assertEqual(self.relay(store, publisher).run_once(), 1)

        self.assertEqual(store.published, [event.event_id])
        [(body, key)] = publisher.sent
        self.assertEqual(key, event.payload["user_id"])
        self.assertNotEqual(key, str(event.aggregate_id))
        self.assertEqual(body, build_envelope(event))
        self.assertEqual(body["producer"], "third-party-management")
        self.assertEqual(body["aggregate_type"], "external_activity")
        self.assertEqual(body["occurred_at"], "2026-10-25T17:00:00Z")
        self.assertEqual(body["causation_id"], event.causation_id)
        self.assertEqual(body["correlation_id"], str(event.correlation_id))
        self.assertEqual(body["payload"], event.payload)

    def test_envelope_without_correlation(self) -> None:
        event = replace(claimed(), correlation_id=None)

        self.assertIsNone(build_envelope(event)["correlation_id"])

    def test_ordering_key_falls_back_to_the_aggregate(self) -> None:
        event = replace(claimed(), payload={})

        self.assertEqual(ordering_key(event), str(event.aggregate_id))

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
        self.assertEqual(
            (record.getMessage(), record.levelno), ("outbox.publish_failed", logging.WARNING)
        )

    def test_alert_from_the_tenth_failed_attempt(self) -> None:
        ninth, tenth, later = claimed(attempts=8), claimed(attempts=9), claimed(attempts=40)
        store = FakeOutboxStore([ninth, tenth, later])

        with self.assertLogs("sportapp.outbox", level="WARNING") as logs:
            self.relay(store, FailingPublisher()).run_once()

        self.assertEqual(
            [(record.getMessage(), record.levelno) for record in logs.records],
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

        fields = str(logs.records[0].fields)  # type: ignore[attr-defined]
        for value in (event.payload["user_id"], event.payload["name"]):
            self.assertNotIn(value, fields)

    def test_published_events_are_purged_at_most_once_per_hour(self) -> None:
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


class PubSubSetupTest(unittest.TestCase):
    def test_refuses_to_run_outside_the_emulator(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(ConfigurationError):
            ensure_resources("sportapp-local")


if __name__ == "__main__":
    unittest.main()
