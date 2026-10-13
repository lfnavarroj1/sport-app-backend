"""HU032 contra el emulador de Pub/Sub y PostgreSQL reales (CA8).

Inicio y fin → outbox → `sportapp.entrenamientos.v1` (orden por `session_id`) →
`proyector-entrenamientos-v1` → sesión activa y luego historial, una sola vez aunque se
repita la entrega. Requiere `TEST_DATABASE_URL` y `PUBSUB_EMULATOR_HOST`.
"""

import contextlib
import json
import os
import time
import unittest
import uuid
from collections.abc import Callable
from pathlib import Path

from google.cloud import pubsub_v1
from jsonschema import Draft202012Validator
from support import athlete_and_professional, finish_command, start_command, training_databases
from test_messaging import wait_until

from sportapp_platform.testing.contracts import repository_root
from training_management.infrastructure.messaging.pubsub import PubSubEventPublisher
from training_management.infrastructure.persistence_read.models import (
    ActiveTrainingSessionRow,
    ProcessedEventRow,
    TrainingSessionHistoryRow,
)
from training_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from training_management.settings import (
    PROJECTOR_SUBSCRIPTION,
    TRAINING_TOPIC,
    OutboxPublisherSettings,
)
from training_management.workers.outbox_publisher import OutboxRelay
from training_management.workers.pubsub_setup import ensure_resources
from training_management.workers.read_projector import ProjectionMessageHandler

SCHEMAS = repository_root(Path(__file__).resolve()) / "contracts" / "events" / "entrenamientos"


@unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
class SessionMessagingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        self.db = self.enterContext(training_databases("hu032_pubsub"))
        ensure_resources(self.project)
        self.subscriber = pubsub_v1.SubscriberClient()
        self.addCleanup(self.subscriber.close)
        self.athlete, _ = athlete_and_professional()

    def publish_outbox(self) -> int:
        settings = OutboxPublisherSettings(
            app_env="test",  # type: ignore[arg-type]
            database_write_url=self.db.write_url,  # type: ignore[arg-type]
            pubsub_project_id=self.project,
            worker_id="w-test",
        )
        publisher = PubSubEventPublisher(self.project, TRAINING_TOPIC)
        try:
            return OutboxRelay(
                SqlAlchemyOutboxStore(self.db.writes), publisher, settings
            ).run_once()
        finally:
            publisher.close()

    def consume(self, until: Callable[[], bool]) -> bool:
        path = self.subscriber.subscription_path(self.project, PROJECTOR_SUBSCRIPTION)
        streaming = self.subscriber.subscribe(
            path, callback=ProjectionMessageHandler(self.db.project())
        )
        try:
            return wait_until(until)
        finally:
            streaming.cancel()
            with contextlib.suppress(Exception):
                streaming.result(timeout=10)

    def test_session_reaches_active_view_then_history_once(self) -> None:
        listener = self.subscriber.subscription_path(self.project, f"test-{uuid.uuid4().hex[:6]}")
        self.subscriber.create_subscription(
            request={
                "name": listener,
                "topic": f"projects/{self.project}/topics/{TRAINING_TOPIC}",
                "enable_message_ordering": True,
            }
        )
        session_id = self.db.start_session()(self.athlete, start_command()).session.session_id

        self.assertEqual(self.publish_outbox(), 1)
        projected = self.consume(lambda: self.db.count(ActiveTrainingSessionRow, read=True) == 1)
        self.assertTrue(projected, "el inicio no llegó a la sesión activa")
        active = self.db.active_reader().current(self.athlete.user_id)
        assert active is not None
        self.assertEqual(active.session_id, session_id)

        self.db.finish_session()(self.athlete, finish_command(session_id))
        self.assertEqual(self.publish_outbox(), 1)
        received = self.subscriber.pull(
            request={"subscription": listener, "max_messages": 10}, timeout=30
        ).received_messages
        bodies = {json.loads(item.message.data)["event_type"]: item.message for item in received}
        for event_type, message in bodies.items():
            schema = json.loads((SCHEMAS / f"{event_type}.schema.json").read_text("utf-8"))
            Draft202012Validator(schema).validate(json.loads(message.data))
            self.assertEqual(message.ordering_key, str(session_id))

        # Entrega duplicada del fin (al menos una vez).
        publisher = pubsub_v1.PublisherClient(
            publisher_options=pubsub_v1.types.PublisherOptions(enable_message_ordering=True)
        )
        try:
            finished = bodies["entrenamiento.finalizado.v1"]
            publisher.publish(
                publisher.topic_path(self.project, TRAINING_TOPIC),
                finished.data,
                ordering_key=finished.ordering_key,
            ).result(timeout=30)
        finally:
            publisher.stop()

        done = self.consume(lambda: self.db.count(ProcessedEventRow, read=True) == 2)
        time.sleep(1.0)  # margen para que la entrega duplicada también se procese

        self.assertTrue(done, "el fin no llegó al historial")
        self.assertEqual(self.db.count(ActiveTrainingSessionRow, read=True), 0)
        self.assertEqual(self.db.count(TrainingSessionHistoryRow, read=True), 1)
        self.assertEqual(self.db.count(ProcessedEventRow, read=True), 2)


if __name__ == "__main__":
    unittest.main()
