"""CQRS de HU010 contra el emulador de Pub/Sub y PostgreSQL reales.

Outbox → `sportapp.entrenamientos.v1` → `proyector-entrenamientos-v1` → vista de
lectura. Requiere `TEST_DATABASE_URL` y `PUBSUB_EMULATOR_HOST`; cada prueba usa
un proyecto propio del emulador.
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
from support import athlete_and_professional, plan_command, training_databases

from sportapp_platform.testing.contracts import repository_root
from training_management.infrastructure.messaging.pubsub import PubSubEventPublisher
from training_management.infrastructure.persistence_read.models import (
    ProcessedEventRow,
    TrainingPlanViewRow,
)
from training_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from training_management.settings import (
    MAX_DELIVERY_ATTEMPTS,
    PROJECTOR_DLQ_TOPIC,
    PROJECTOR_SUBSCRIPTION,
    TRAINING_TOPIC,
    OutboxPublisherSettings,
)
from training_management.workers.outbox_publisher import OutboxRelay
from training_management.workers.pubsub_setup import ensure_resources
from training_management.workers.read_projector import ProjectionMessageHandler

SCHEMA = (
    repository_root(Path(__file__).resolve())
    / "contracts"
    / "events"
    / "entrenamientos"
    / "plan.entrenamiento_creado.v1.schema.json"
)


def wait_until(condition: Callable[[], bool], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


@unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
class TrainingMessagingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        self.db = self.enterContext(training_databases("hu010_pubsub"))
        self.created = ensure_resources(self.project)
        self.subscriber = pubsub_v1.SubscriberClient()
        self.addCleanup(self.subscriber.close)

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

    def test_setup_creates_ordered_subscription_with_dlq(self) -> None:
        names = {path.rsplit("/", 1)[-1] for path in self.created}
        subscription = self.subscriber.get_subscription(
            subscription=self.subscriber.subscription_path(self.project, PROJECTOR_SUBSCRIPTION)
        )

        self.assertEqual(
            names,
            {
                TRAINING_TOPIC,
                PROJECTOR_DLQ_TOPIC,
                PROJECTOR_SUBSCRIPTION,
                "sportapp.entrenamientos.proyeccion.dlq.ops",
            },
        )
        self.assertTrue(subscription.enable_message_ordering)
        self.assertEqual(
            subscription.dead_letter_policy.max_delivery_attempts, MAX_DELIVERY_ATTEMPTS
        )
        self.assertTrue(
            subscription.dead_letter_policy.dead_letter_topic.endswith(PROJECTOR_DLQ_TOPIC)
        )
        self.assertEqual(ensure_resources(self.project), self.created, "repetirlo es inofensivo")

    def test_created_plan_reaches_the_read_model_once(self) -> None:
        """CA4: evento publicado con el esquema versionado y vista consultable; una
        entrega duplicada no duplica la vista."""
        listener = self.subscriber.subscription_path(self.project, f"test-{uuid.uuid4().hex[:6]}")
        self.subscriber.create_subscription(
            request={
                "name": listener,
                "topic": f"projects/{self.project}/topics/{TRAINING_TOPIC}",
                "enable_message_ordering": True,
            }
        )
        athlete, professional = athlete_and_professional()
        self.db.grant()(athlete, professional.user_id)
        result = self.db.create_plan()(professional, plan_command(athlete.user_id))

        self.assertEqual(self.publish_outbox(), 1)
        received = self.subscriber.pull(
            request={"subscription": listener, "max_messages": 10}, timeout=30
        ).received_messages
        message = received[0].message
        body = json.loads(message.data)
        Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8"))).validate(body)
        self.assertEqual(message.ordering_key, str(result.plan.plan_id))
        self.assertEqual(message.attributes["event_type"], "plan.entrenamiento_creado.v1")

        # Entrega duplicada del mismo evento (al menos una vez).
        publisher = pubsub_v1.PublisherClient(
            publisher_options=pubsub_v1.types.PublisherOptions(enable_message_ordering=True)
        )
        try:
            publisher.publish(
                publisher.topic_path(self.project, TRAINING_TOPIC),
                message.data,
                ordering_key=message.ordering_key,
            ).result(timeout=30)
        finally:
            publisher.stop()

        projected = self.consume(lambda: self.db.count(ProcessedEventRow, read=True) == 1)
        time.sleep(1.0)  # margen para que la entrega duplicada también se procese

        self.assertTrue(projected, "el proyector no construyó la vista")
        self.assertEqual(self.db.count(TrainingPlanViewRow, read=True), 1)
        self.assertEqual(self.db.reader().get(result.plan.plan_id), result.plan)


if __name__ == "__main__":
    unittest.main()
