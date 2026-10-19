"""CA4: `usuario.registrado.v1` llega a Pub/Sub (emulador) con el contrato aprobado.

Requiere `PUBSUB_EMULATOR_HOST` (`scripts/task.py pubsub-up`); si falta, falla.
"""

import json
import os
import unittest
from pathlib import Path
from uuid import uuid4

from google.cloud import pubsub_v1
from jsonschema import Draft202012Validator, FormatChecker
from support import registration, write_database

from sportapp_platform.testing.contracts import repository_root
from users_management.infrastructure.messaging.pubsub import PubSubEventPublisher
from users_management.infrastructure.persistence_write.models import OutboxEventRow
from users_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from users_management.settings import OutboxPublisherSettings
from users_management.workers.outbox_publisher import OutboxRelay
from users_management.workers.pubsub_setup import ensure_topic

PROJECT_ID = "sportapp-test"
SCHEMA_PATH = Path("contracts/events/usuarios/usuario.registrado.v1.schema.json")


class OutboxPublisherEmulatorTest(unittest.TestCase):
    def setUp(self) -> None:
        if not os.environ.get("PUBSUB_EMULATOR_HOST"):
            self.fail("Falta PUBSUB_EMULATOR_HOST: las pruebas de publicación usan el emulador.")
        self.topic = f"sportapp.usuarios.v1.it-{uuid4().hex[:8]}"
        topic_path = ensure_topic(PROJECT_ID, self.topic)
        self.subscriber = pubsub_v1.SubscriberClient()
        self.subscription = self.subscriber.subscription_path(PROJECT_ID, f"it-{uuid4().hex[:8]}")
        self.subscriber.create_subscription(
            request={
                "name": self.subscription,
                "topic": topic_path,
                "enable_message_ordering": True,
            }
        )
        self.addCleanup(self.subscriber.close)
        self.db = self.enterContext(write_database("users_publisher"))

    def test_registered_user_event_is_published_once_with_contract(self) -> None:
        cmd = registration()
        result = self.db.register_user()(cmd)
        settings = OutboxPublisherSettings(
            app_env="test",  # type: ignore[arg-type]
            database_write_url=self.db.url,  # type: ignore[arg-type]
            pubsub_project_id=PROJECT_ID,
            pubsub_topic=self.topic,
            worker_id="it-worker",
        )
        publisher = PubSubEventPublisher(PROJECT_ID, self.topic)
        self.addCleanup(publisher.close)
        relay = OutboxRelay(SqlAlchemyOutboxStore(self.db.sessions), publisher, settings)

        self.assertEqual(relay.run_once(), 1)
        self.assertEqual(relay.run_once(), 0)

        response = self.subscriber.pull(
            request={"subscription": self.subscription, "max_messages": 10}, timeout=30
        )
        [received] = response.received_messages
        message = received.message
        envelope = json.loads(message.data)
        schema = json.loads(
            (repository_root(Path(__file__).resolve()) / SCHEMA_PATH).read_text(encoding="utf-8")
        )
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(envelope)
        self.assertEqual(message.ordering_key, str(result.user_id))
        self.assertEqual(message.attributes["event_type"], "usuario.registrado.v1")
        # R-19 (ADR-034): el tipo de agregado también viaja como atributo para filtrar.
        self.assertEqual(message.attributes["aggregate_type"], "user_account")
        self.assertEqual(envelope["aggregate_id"], str(result.user_id))
        self.assertEqual(envelope["correlation_id"], str(cmd.correlation_id))
        self.assertEqual(envelope["causation_id"], str(cmd.idempotency_key))
        self.assertNotIn(cmd.password, message.data.decode("utf-8"))
        with self.db.sessions() as session:
            stored = session.get_one(OutboxEventRow, envelope["event_id"])
        self.assertIsNotNone(stored.published_at)


if __name__ == "__main__":
    unittest.main()
