"""`terceros-outbox-publisher` contra PostgreSQL y el emulador de Pub/Sub reales (HU031,
ADR-042 §4, CMP-002).

Requiere `TEST_DATABASE_URL` y `PUBSUB_EMULATOR_HOST`. Cada prueba usa un proyecto propio del
emulador; el tópico `sportapp.terceros.v1` lo crea `pubsub_setup.ensure_resources` y la
suscripción de lectura es de la prueba (la del consumidor real la posee Entrenamientos). Las
actividades llegan a la outbox por una corrida real de `RunSync` con el simulador de Strava.
"""

import base64
import json
import os
import secrets
import time
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from google.api_core.exceptions import DeadlineExceeded
from google.cloud import pubsub_v1
from jsonschema import Draft202012Validator
from test_sync_flow import (
    ThirdPartyDatabase,
    build_sync,
    new_token_key,
    queue_run,
    seed_account,
    strava_context,
    synthetic_activities,
    third_party_database,
)

from sportapp_platform.config import ConfigurationError
from sportapp_platform.testing.contracts import repository_root
from third_party_management.infrastructure.messaging.pubsub import PubSubEventPublisher
from third_party_management.infrastructure.persistence.models import OutboxEventRow
from third_party_management.infrastructure.persistence.outbox import SqlAlchemyOutboxStore
from third_party_management.settings import TERCEROS_TOPIC, OutboxPublisherSettings
from third_party_management.testing.strava_simulator import StravaSimulator
from third_party_management.workers.outbox_publisher import OutboxRelay
from third_party_management.workers.pubsub_setup import ensure_resources

SCHEMA = (
    repository_root(Path(__file__).resolve())
    / "contracts"
    / "events"
    / "terceros"
    / "actividad.importada.v1.schema.json"
)
READER_SUBSCRIPTION = "prueba-terceros-lectura"
ENVELOPE_FIELDS = {
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
}
RELAY_WORKER = "terceros-outbox-it"


class _FailingPublisher:
    def publish(self, envelope: dict[str, Any], ordering_key: str) -> None:
        raise ConnectionError("emulador no disponible")


@unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
class OutboxMessagingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        self.db: ThirdPartyDatabase = self.enterContext(third_party_database("tpm_outbox"))
        self.created = ensure_resources(self.project)
        self.subscriber = pubsub_v1.SubscriberClient()
        self.addCleanup(self.subscriber.close)
        self.subscription = self.subscriber.subscription_path(self.project, READER_SUBSCRIPTION)
        self.subscriber.create_subscription(
            request={
                "name": self.subscription,
                "topic": self.created[0],
                "enable_message_ordering": True,
            }
        )
        values: dict[str, Any] = {
            "app_env": "test",
            "database_url": self.db.url,
            "pubsub_project_id": self.project,
            "worker_id": RELAY_WORKER,
        }
        self.settings = OutboxPublisherSettings(**values)
        self.user_id = uuid.uuid4()
        self.strava = StravaSimulator()
        self.client_secret = secrets.token_hex(16)
        self.context = strava_context(
            self.strava, base64.urlsafe_b64decode(new_token_key()), self.client_secret
        )
        seed_account(
            self.db,
            self.context,
            self.user_id,
            self.strava.issue_tokens(),
            datetime.now(UTC) + timedelta(hours=6),
        )

    def import_activities(self, count: int) -> uuid.UUID:
        self.strava.activities = synthetic_activities(7001, count)
        run = queue_run(self.db, self.user_id)
        outcome = build_sync(self.db, self.context)()
        assert outcome is not None
        self.assertEqual((outcome.status, outcome.imported_count), ("SUCCEEDED", count))
        return run.sync_run_id

    def relay(self, publisher: Any = None) -> int:
        real = publisher is None
        publisher = publisher or PubSubEventPublisher(self.project, TERCEROS_TOPIC)
        try:
            return OutboxRelay(
                SqlAlchemyOutboxStore(self.db.sessions), publisher, self.settings
            ).run_once()
        finally:
            if real:
                publisher.close()

    def pull(self, expected: int, timeout: float = 30.0) -> list[Any]:
        received: list[Any] = []
        deadline = time.monotonic() + timeout
        while len(received) < expected and time.monotonic() < deadline:
            try:
                response = self.subscriber.pull(
                    request={"subscription": self.subscription, "max_messages": 10}, timeout=5
                )
            except DeadlineExceeded:
                # El emulador retiene la solicitud mientras no hay mensajes.
                continue
            received.extend(response.received_messages)
        if received:
            self.subscriber.acknowledge(
                request={
                    "subscription": self.subscription,
                    "ack_ids": [item.ack_id for item in received],
                }
            )
        return [item.message for item in received]

    def test_setup_creates_only_the_topic_and_refuses_outside_the_emulator(self) -> None:
        self.assertEqual(
            self.created, [f"projects/{self.project}/topics/{TERCEROS_TOPIC}"], "solo el tópico"
        )
        self.assertEqual(ensure_resources(self.project), self.created, "repetirlo es inofensivo")
        emulator = os.environ.pop("PUBSUB_EMULATOR_HOST")
        try:
            with self.assertRaises(ConfigurationError):
                ensure_resources(self.project)
        finally:
            os.environ["PUBSUB_EMULATOR_HOST"] = emulator

    def test_relay_publishes_the_full_envelope_ordered_by_user(self) -> None:
        """CA4: sobre completo y válido contra el esquema, `ordering_key = user_id`, atributos
        del patrón y fila marcada como publicada solo tras la confirmación."""
        sync_run_id = self.import_activities(2)
        rows = {row.event_id: row for row in self.db.rows(OutboxEventRow)}

        published = self.relay()
        messages = self.pull(expected=2)

        self.assertEqual(published, 2)
        self.assertEqual(len(messages), 2)
        validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
        for message in messages:
            envelope = json.loads(message.data)
            self.assertEqual(set(envelope), ENVELOPE_FIELDS)
            self.assertEqual(list(validator.iter_errors(envelope)), [])
            row = rows[uuid.UUID(envelope["event_id"])]
            self.assertEqual(envelope["producer"], "third-party-management")
            self.assertEqual(envelope["event_type"], "actividad.importada.v1")
            self.assertEqual(envelope["aggregate_type"], "external_activity")
            self.assertEqual(envelope["aggregate_id"], str(row.aggregate_id))
            self.assertEqual(envelope["aggregate_version"], 1)
            self.assertEqual(envelope["causation_id"], str(sync_run_id))
            self.assertEqual(envelope["correlation_id"], str(row.correlation_id))
            self.assertEqual(envelope["schema_version"], 1)
            self.assertEqual(envelope["payload"], row.payload)
            self.assertEqual(message.ordering_key, str(self.user_id))
            self.assertEqual(
                dict(message.attributes),
                {
                    "event_id": envelope["event_id"],
                    "event_type": "actividad.importada.v1",
                    "schema_version": "1",
                    "correlation_id": envelope["correlation_id"],
                    "aggregate_type": "external_activity",
                },
            )
            serialized = message.data.decode("utf-8")
            for value in [*self.strava.sensitive_values(), self.client_secret]:
                self.assertNotIn(value, serialized)
        for row in self.db.rows(OutboxEventRow):
            self.assertIsNotNone(row.published_at)
            self.assertEqual((row.lease_owner, row.lease_until, row.attempts), (None, None, 0))
        self.assertEqual(self.relay(), 0, "un evento publicado no se vuelve a publicar")

    def test_failed_publication_keeps_the_event_pending_with_backoff(self) -> None:
        """CMP-002: un fallo incrementa `attempts`, reprograma de 10 a 600 s y no publica."""
        self.import_activities(1)

        self.assertEqual(self.relay(_FailingPublisher()), 1)

        [row] = self.db.rows(OutboxEventRow)
        self.assertIsNone(row.published_at)
        self.assertEqual((row.attempts, row.last_error), (1, "ConnectionError"))
        self.assertGreater(row.next_attempt_at, datetime.now(UTC) + timedelta(seconds=5))
        self.assertEqual(self.relay(), 0, "no se reintenta antes de su próximo intento")
        self.assertEqual(self.pull(expected=1, timeout=3), [])


if __name__ == "__main__":
    unittest.main()
