"""CQRS de HU021 y HU024 contra el emulador de Pub/Sub y PostgreSQL reales (CA5).

Outbox → `sportapp.eventos.v1` → `proyector-eventos-v1` → `event_read`. Requiere
`TEST_DATABASE_URL` y `PUBSUB_EMULATOR_HOST`; cada prueba usa un proyecto propio del
emulador.
"""

import contextlib
import json
import os
import time
import unittest
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from google.cloud import pubsub_v1
from jsonschema import Draft202012Validator
from support import event_command, event_databases, publish_command

from events_management.application.ports import Caller
from events_management.infrastructure.messaging.pubsub import PubSubEventPublisher
from events_management.infrastructure.persistence_read.models import (
    EventReadRow,
    ProcessedEventRow,
)
from events_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from events_management.settings import (
    EVENTS_TOPIC,
    MAX_DELIVERY_ATTEMPTS,
    NOTIFICATIONS_DLQ_OPS_SUBSCRIPTION,
    NOTIFICATIONS_DLQ_TOPIC,
    NOTIFICATIONS_FILTER,
    NOTIFICATIONS_SUBSCRIPTION,
    PROJECTOR_DLQ_OPS_SUBSCRIPTION,
    PROJECTOR_DLQ_TOPIC,
    PROJECTOR_SUBSCRIPTION,
    RETRY_MAX_BACKOFF_SECONDS,
    RETRY_MIN_BACKOFF_SECONDS,
    OutboxPublisherSettings,
)
from events_management.workers.outbox_publisher import OutboxRelay
from events_management.workers.pubsub_setup import ensure_resources
from events_management.workers.read_projector import ProjectionMessageHandler
from sportapp_platform.testing.contracts import repository_root

SCHEMAS = repository_root(Path(__file__).resolve()) / "contracts" / "events" / "eventos"
SCHEMA = SCHEMAS / "evento.creado.v1.schema.json"
PUBLISHED_SCHEMA = SCHEMAS / "evento.publicado.v1.schema.json"


def wait_until(condition: Callable[[], bool], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


@unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
class EventsMessagingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        self.db = self.enterContext(event_databases("hu021_pubsub"))
        self.created = ensure_resources(self.project)
        self.subscriber = pubsub_v1.SubscriberClient()
        self.addCleanup(self.subscriber.close)
        self.organizer = Caller(uuid.uuid4(), "organizer")

    def publish_outbox(self) -> int:
        settings = OutboxPublisherSettings(
            app_env="test",  # type: ignore[arg-type]
            database_write_url=self.db.write_url,  # type: ignore[arg-type]
            pubsub_project_id=self.project,
            worker_id="w-test",
        )
        publisher = PubSubEventPublisher(self.project, EVENTS_TOPIC)
        try:
            return OutboxRelay(
                SqlAlchemyOutboxStore(self.db.writes), publisher, settings
            ).run_once()
        finally:
            publisher.close()

    def consume(
        self,
        callback: Callable[[Any], None],
        until: Callable[[], bool],
        timeout: float = 30.0,
        subscription: str = PROJECTOR_SUBSCRIPTION,
    ) -> bool:
        path = self.subscriber.subscription_path(self.project, subscription)
        streaming = self.subscriber.subscribe(path, callback=callback)
        try:
            return wait_until(until, timeout)
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
                EVENTS_TOPIC,
                PROJECTOR_DLQ_TOPIC,
                PROJECTOR_SUBSCRIPTION,
                PROJECTOR_DLQ_OPS_SUBSCRIPTION,
                NOTIFICATIONS_DLQ_TOPIC,
                NOTIFICATIONS_SUBSCRIPTION,
                NOTIFICATIONS_DLQ_OPS_SUBSCRIPTION,
            },
        )
        self.assertTrue(subscription.enable_message_ordering)
        self.assertEqual(
            subscription.dead_letter_policy.max_delivery_attempts, MAX_DELIVERY_ATTEMPTS
        )
        self.assertTrue(
            subscription.dead_letter_policy.dead_letter_topic.endswith(PROJECTOR_DLQ_TOPIC)
        )
        self.assertEqual(
            (
                subscription.retry_policy.minimum_backoff.total_seconds(),
                subscription.retry_policy.maximum_backoff.total_seconds(),
            ),
            (RETRY_MIN_BACKOFF_SECONDS, RETRY_MAX_BACKOFF_SECONDS),
        )
        self.assertEqual(ensure_resources(self.project), self.created, "repetirlo es inofensivo")

    def test_notifications_subscription_is_filtered_with_its_own_dlq(self) -> None:
        """HU039 (ADR-045 §8): `eventos-notificaciones-v1` solo recibe `evento.publicado.v1`."""
        subscription = self.subscriber.get_subscription(
            subscription=self.subscriber.subscription_path(self.project, NOTIFICATIONS_SUBSCRIPTION)
        )
        self.assertTrue(subscription.enable_message_ordering)
        self.assertEqual(subscription.filter, NOTIFICATIONS_FILTER)
        self.assertTrue(
            subscription.dead_letter_policy.dead_letter_topic.endswith(NOTIFICATIONS_DLQ_TOPIC)
        )
        created = self.db.create_event()(self.organizer, event_command())
        self.db.publish()(self.organizer, publish_command(created.event.event_id))
        self.assertEqual(self.publish_outbox(), 2)
        received: list[str] = []

        def collect(message: Any) -> None:
            received.append(json.loads(message.data)["event_type"])
            message.ack()

        self.assertTrue(
            self.consume(collect, lambda: bool(received), subscription=NOTIFICATIONS_SUBSCRIPTION)
        )
        time.sleep(2)  # un `evento.creado.v1` que pasara el filtro ya habría llegado
        self.assertEqual(received, ["evento.publicado.v1"])

    def test_created_event_reaches_the_read_model_once(self) -> None:
        """CA5: evento publicado con el esquema versionado, `ordering_key = event_id`, y una
        entrega duplicada que no duplica la vista."""
        listener = self.subscriber.subscription_path(self.project, f"test-{uuid.uuid4().hex[:6]}")
        self.subscriber.create_subscription(
            request={
                "name": listener,
                "topic": f"projects/{self.project}/topics/{EVENTS_TOPIC}",
                "enable_message_ordering": True,
            }
        )
        result = self.db.create_event()(self.organizer, event_command())

        self.assertEqual(self.publish_outbox(), 1)
        received = self.subscriber.pull(
            request={"subscription": listener, "max_messages": 10}, timeout=30
        ).received_messages
        message = received[0].message
        body = json.loads(message.data)
        Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8"))).validate(body)
        self.assertEqual(message.ordering_key, str(result.event.event_id))
        self.assertEqual(message.attributes["event_type"], "evento.creado.v1")

        # Entrega duplicada del mismo mensaje (al menos una vez).
        publisher = pubsub_v1.PublisherClient(
            publisher_options=pubsub_v1.types.PublisherOptions(enable_message_ordering=True)
        )
        try:
            publisher.publish(
                publisher.topic_path(self.project, EVENTS_TOPIC),
                message.data,
                ordering_key=message.ordering_key,
            ).result(timeout=30)
        finally:
            publisher.stop()

        projected = self.consume(
            ProjectionMessageHandler(self.db.project()),
            lambda: self.db.count(ProcessedEventRow, read=True) == 1,
        )
        time.sleep(1.0)  # margen para que la entrega duplicada también se procese

        self.assertTrue(projected, "el proyector no construyó la vista")
        self.assertEqual(self.db.count(EventReadRow, read=True), 1)
        self.assertEqual(self.db.count(ProcessedEventRow, read=True), 1)
        self.assertEqual(
            self.db.reader().owned_by(self.organizer.user_id, result.event.event_id),
            result.event,
        )

    def test_projector_recovers_the_backlog_after_a_failure(self) -> None:
        """Recuperación básica: con el proyector fallando nada se confirma; al reiniciarlo,
        el backlog se proyecta completo."""
        created = [self.db.create_event()(self.organizer, event_command()) for _ in range(3)]
        self.assertEqual(self.publish_outbox(), 3)
        failures: list[str] = []

        def failing(message: Any) -> None:
            failures.append(message.message_id)
            message.nack()

        self.consume(failing, lambda: len(failures) >= 3)
        self.assertEqual(self.db.count(EventReadRow, read=True), 0)

        recovered = self.consume(
            ProjectionMessageHandler(self.db.project()),
            lambda: self.db.count(EventReadRow, read=True) == len(created),
            timeout=90.0,  # los mensajes rechazados vuelven tras la espera mínima de 10 s
        )

        self.assertTrue(recovered, "el backlog no se proyectó tras reiniciar el proyector")
        reader = self.db.reader()
        for result in created:
            self.assertIsNotNone(reader.owned_by(self.organizer.user_id, result.event.event_id))

    def test_published_event_reaches_the_read_model_after_a_restart(self) -> None:
        """HU024 CA1 y CA5: `evento.publicado.v1` con el esquema versionado y
        `ordering_key = event_id`; con el proyector caído no se confirma nada y, al
        reiniciarlo, `event_read` termina `PUBLISHED` (versión 2) una sola vez."""
        listener = self.subscriber.subscription_path(self.project, f"test-{uuid.uuid4().hex[:6]}")
        self.subscriber.create_subscription(
            request={
                "name": listener,
                "topic": f"projects/{self.project}/topics/{EVENTS_TOPIC}",
                "enable_message_ordering": True,
            }
        )
        created = self.db.create_event()(self.organizer, event_command())
        event_id = created.event.event_id
        self.db.publish()(self.organizer, publish_command(event_id))

        self.assertEqual(self.publish_outbox(), 2)
        received = self.subscriber.pull(
            request={"subscription": listener, "max_messages": 10}, timeout=30
        ).received_messages
        published = [
            m.message
            for m in received
            if m.message.attributes["event_type"] == "evento.publicado.v1"
        ]
        self.assertEqual(len(published), 1)
        body = json.loads(published[0].data)
        Draft202012Validator(json.loads(PUBLISHED_SCHEMA.read_text(encoding="utf-8"))).validate(
            body
        )
        self.assertEqual(published[0].ordering_key, str(event_id))
        self.assertEqual(body["aggregate_version"], 2)

        failures: list[str] = []

        def failing(message: Any) -> None:
            failures.append(message.message_id)
            message.nack()

        self.consume(failing, lambda: len(failures) >= 1)
        self.assertEqual(self.db.count(EventReadRow, read=True), 0)

        recovered = self.consume(
            ProjectionMessageHandler(self.db.project()),
            lambda: self.db.count(ProcessedEventRow, read=True) == 2,
            timeout=90.0,
        )

        self.assertTrue(recovered, "el proyector no aplicó la publicación tras reiniciarse")
        with self.db.reads() as session:
            row = session.get(EventReadRow, event_id)
        assert row is not None
        self.assertEqual((row.status, row.version), ("PUBLISHED", 2))
        self.assertIsNotNone(row.published_at)


if __name__ == "__main__":
    unittest.main()
