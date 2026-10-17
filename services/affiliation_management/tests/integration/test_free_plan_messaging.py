"""Mensajería de ADR-024 contra el emulador de Pub/Sub y PostgreSQL reales.

Requiere `TEST_DATABASE_URL` y `PUBSUB_EMULATOR_HOST`. Cada prueba usa un proyecto
propio del emulador para no compartir tópicos ni suscripciones.
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
from sqlalchemy.orm import sessionmaker
from support import affiliation_database, user_registered

from affiliation_management.infrastructure.messaging.pubsub import PubSubEventPublisher
from affiliation_management.infrastructure.persistence.models import SubscriptionRow
from affiliation_management.infrastructure.persistence.outbox import SqlAlchemyOutboxStore
from affiliation_management.settings import (
    AFFILIATION_TOPIC,
    FREE_PLAN_FILTER,
    FREE_PLAN_SUBSCRIPTION,
    MAX_DELIVERY_ATTEMPTS,
    USERS_TOPIC,
    OutboxPublisherSettings,
)
from affiliation_management.workers.free_plan_consumer import FreePlanMessageHandler
from affiliation_management.workers.outbox_publisher import OutboxRelay
from affiliation_management.workers.pubsub_setup import ensure_resources
from sportapp_platform.config import ConfigurationError
from sportapp_platform.testing.contracts import repository_root

SCHEMA = (
    repository_root(Path(__file__).resolve())
    / "contracts"
    / "events"
    / "afiliacion"
    / "suscripcion.activada.v1.schema.json"
)


def wait_until(condition: Callable[[], bool], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


@unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
class FreePlanMessagingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        self.db = self.enterContext(affiliation_database("affiliation_pubsub"))
        self.created = ensure_resources(self.project)
        self.publisher = pubsub_v1.PublisherClient(
            publisher_options=pubsub_v1.types.PublisherOptions(enable_message_ordering=True)
        )
        self.addCleanup(self.publisher.stop)
        self.subscriber = pubsub_v1.SubscriberClient()
        self.addCleanup(self.subscriber.close)

    def publish_user_registered(self, envelope: dict[str, object]) -> None:
        """Con los atributos que pone `usuarios-outbox-publisher` (R-19, ADR-034)."""
        topic = self.publisher.topic_path(self.project, USERS_TOPIC)
        self.publisher.publish(
            topic,
            json.dumps(envelope).encode(),
            ordering_key=str(envelope["aggregate_id"]),
            event_type=str(envelope["event_type"]),
            aggregate_type=str(envelope.get("aggregate_type", "user_account")),
        ).result(timeout=30)

    def consume(self, until: Callable[[], bool]) -> bool:
        path = self.subscriber.subscription_path(self.project, FREE_PLAN_SUBSCRIPTION)
        streaming = self.subscriber.subscribe(
            path, callback=FreePlanMessageHandler(self.db.provision())
        )
        try:
            return wait_until(until)
        finally:
            streaming.cancel()
            # Tras cancelar, `result` puede relanzar la cancelación; solo se espera el cierre.
            with contextlib.suppress(Exception):
                streaming.result(timeout=10)

    def test_setup_creates_the_approved_resources_with_dlq_policy(self) -> None:
        names = {path.rsplit("/", 1)[-1] for path in self.created}
        subscription = self.subscriber.get_subscription(
            subscription=self.subscriber.subscription_path(self.project, FREE_PLAN_SUBSCRIPTION)
        )

        self.assertEqual(
            names,
            {
                USERS_TOPIC,
                AFFILIATION_TOPIC,
                "sportapp.afiliacion.alta-gratuita.dlq",
                FREE_PLAN_SUBSCRIPTION,
                "sportapp.afiliacion.alta-gratuita.dlq.ops",
            },
        )
        self.assertTrue(subscription.enable_message_ordering)
        self.assertEqual(subscription.filter, FREE_PLAN_FILTER)
        self.assertEqual(
            subscription.dead_letter_policy.max_delivery_attempts, MAX_DELIVERY_ATTEMPTS
        )
        self.assertTrue(
            subscription.dead_letter_policy.dead_letter_topic.endswith(
                "sportapp.afiliacion.alta-gratuita.dlq"
            )
        )
        self.assertEqual(ensure_resources(self.project), self.created, "repetirlo es inofensivo")

    def test_profile_events_never_reach_the_free_plan_subscription(self) -> None:
        """R-19 (ADR-034, HU005 CA7): el filtro deja fuera los `perfil.*` del tópico."""
        registered = user_registered()
        profile = {
            **user_registered(),
            "event_type": "perfil.demografico_actualizado.v1",
            "aggregate_type": "demographic_profile",
        }

        self.publish_user_registered(profile)
        self.publish_user_registered(registered)
        received = self.subscriber.pull(
            request={
                "subscription": self.subscriber.subscription_path(
                    self.project, FREE_PLAN_SUBSCRIPTION
                ),
                "max_messages": 10,
            },
            timeout=30,
        ).received_messages

        self.assertEqual(
            [json.loads(item.message.data)["event_id"] for item in received],
            [registered["event_id"]],
        )

    def test_setup_refuses_to_run_outside_the_emulator(self) -> None:
        emulator = os.environ.pop("PUBSUB_EMULATOR_HOST")
        try:
            with self.assertRaises(ConfigurationError):
                ensure_resources(self.project)
        finally:
            os.environ["PUBSUB_EMULATOR_HOST"] = emulator

    def test_registered_athlete_is_provisioned_once_and_event_is_published(self) -> None:
        """CA6 y CA9 por Pub/Sub: entrega duplicada sin segunda suscripción."""
        envelope = user_registered()
        listener = self.subscriber.subscription_path(self.project, f"test-{uuid.uuid4().hex[:6]}")
        self.subscriber.create_subscription(
            request={
                "name": listener,
                "topic": self.publisher.topic_path(self.project, AFFILIATION_TOPIC),
                "enable_message_ordering": True,
            }
        )

        self.publish_user_registered(envelope)
        self.publish_user_registered(envelope)
        provisioned = self.consume(lambda: self.db.count(SubscriptionRow) == 1)
        time.sleep(1.0)  # margen para que la segunda entrega también se procese

        self.assertTrue(provisioned, "el consumidor no aprovisionó la suscripción")
        self.assertEqual(self.db.count(SubscriptionRow), 1)

        settings = OutboxPublisherSettings(
            app_env="test",  # type: ignore[arg-type]
            database_url=self.db.url,  # type: ignore[arg-type]
            pubsub_project_id=self.project,
            worker_id="w-test",
        )
        publisher = PubSubEventPublisher(self.project, AFFILIATION_TOPIC)
        try:
            published = OutboxRelay(
                SqlAlchemyOutboxStore(sessionmaker(self.db.engine)), publisher, settings
            ).run_once()
        finally:
            publisher.close()
        received = self.subscriber.pull(
            request={"subscription": listener, "max_messages": 10}, timeout=30
        ).received_messages

        self.assertEqual(published, 1)
        self.assertEqual(len(received), 1)
        message = received[0].message
        body = json.loads(message.data)
        Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8"))).validate(body)
        self.assertEqual(message.ordering_key, envelope["aggregate_id"])
        self.assertEqual(body["payload"]["user_id"], envelope["aggregate_id"])
        self.assertEqual(body["causation_id"], envelope["event_id"])
        self.assertEqual(message.attributes["event_type"], "suscripcion.activada.v1")


if __name__ == "__main__":
    unittest.main()
