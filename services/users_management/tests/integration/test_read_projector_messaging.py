"""`usuarios-read-projector` contra el emulador de Pub/Sub y PostgreSQL reales.

`sportapp.usuarios.v1` → `proyector-usuarios-v1` → base de lectura. Requiere
`TEST_DATABASE_URL` y `PUBSUB_EMULATOR_HOST`; cada prueba usa un proyecto propio del
emulador, así que los nombres no chocan entre pruebas.
"""

import contextlib
import json
import os
import threading
import time
import unittest
import uuid
from collections.abc import Callable
from typing import Any

from google.cloud import pubsub_v1
from projection_support import (
    SYNTHETIC_TEMPERATURE,
    SYNTHETIC_WEIGHT,
    profile_envelope,
    read_database,
    registered_envelope,
)

from users_management.application.messages import UsersEvent
from users_management.application.projections.users_projection import ProjectionOutcome
from users_management.domain.profile.events import (
    AGGREGATE_TYPE_BY_SECTION,
    SECTION_BY_EVENT_TYPE,
    ProfileSection,
)
from users_management.infrastructure.messaging.pubsub import PubSubEventPublisher
from users_management.infrastructure.persistence_read.models import (
    AthleteProfileViewRow,
    ProcessedEventRow,
    UserDirectoryRow,
)
from users_management.settings import (
    MAX_DELIVERY_ATTEMPTS,
    PROJECTOR_DLQ_OPS_SUBSCRIPTION,
    PROJECTOR_DLQ_TOPIC,
    PROJECTOR_SUBSCRIPTION,
    RETRY_MAX_BACKOFF_SECONDS,
    RETRY_MIN_BACKOFF_SECONDS,
    USERS_TOPIC,
)
from users_management.workers.pubsub_setup import ensure_resources
from users_management.workers.read_projector import ProjectionMessageHandler


def wait_until(condition: Callable[[], bool], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


@unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
class ReadProjectorMessagingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        self.created = ensure_resources(self.project)
        self.db = self.enterContext(read_database("users_projector_pubsub"))
        self.subscriber = pubsub_v1.SubscriberClient()
        self.addCleanup(self.subscriber.close)

    def publish(self, *envelopes: dict[str, Any]) -> None:
        publisher = PubSubEventPublisher(self.project, USERS_TOPIC)
        try:
            for envelope in envelopes:
                section = SECTION_BY_EVENT_TYPE.get(envelope["event_type"])
                aggregate_type = (
                    AGGREGATE_TYPE_BY_SECTION[section] if section is not None else "user_account"
                )
                publisher.publish(envelope, envelope["aggregate_id"], aggregate_type)
        finally:
            publisher.close()

    def consume(
        self,
        callback: Callable[[Any], None],
        until: Callable[[], bool],
        subscription: str = PROJECTOR_SUBSCRIPTION,
        timeout: float = 30.0,
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
                USERS_TOPIC,
                PROJECTOR_DLQ_TOPIC,
                PROJECTOR_SUBSCRIPTION,
                PROJECTOR_DLQ_OPS_SUBSCRIPTION,
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

    def test_published_events_are_projected_and_acked(self) -> None:
        """CA7: el registro y las dos secciones publicados en el tópico llegan a la vista
        y se confirman; no se vuelven a entregar."""
        user_id = uuid.uuid4()
        registration = registered_envelope(user_id)
        demographic = profile_envelope(ProfileSection.DEMOGRAPHIC, user_id, 1)
        physiological = profile_envelope(ProfileSection.PHYSIOLOGICAL, user_id, 1)
        self.publish(registration, demographic, physiological)
        handler = ProjectionMessageHandler(self.db.projector())

        with self.assertLogs("sportapp.projector", level="INFO") as logs:
            projected = self.consume(handler, lambda: self.db.count(ProcessedEventRow) == 3)

        self.assertTrue(projected, "el proyector no aplicó los tres eventos")
        directory = self.db.directory(user_id)
        assert directory is not None
        self.assertEqual(directory.email, registration["payload"]["email"])
        view = self.db.reader().get(user_id)
        assert view is not None
        self.assertEqual((view.demographic_version, view.physiological_version), (1, 1))
        self.assertEqual(view.physiological["weight_kg"], SYNTHETIC_WEIGHT)  # type: ignore[index]
        rendered = " ".join(f"{r.getMessage()} {getattr(r, 'fields', {})}" for r in logs.records)
        for value in (SYNTHETIC_WEIGHT, SYNTHETIC_TEMPERATURE, "@example.com"):
            self.assertNotIn(value, rendered, "los logs no llevan valores del payload")

        redelivered: list[str] = []

        def count(message: Any) -> None:
            redelivered.append(message.message_id)
            message.ack()

        self.consume(count, lambda: False, timeout=3.0)
        self.assertEqual(redelivered, [], "los mensajes confirmados no vuelven")

    def test_invalid_message_ends_in_the_dlq(self) -> None:
        """Un evento inválido no se confirma y, agotadas las entregas, pasa a la DLQ.

        La suscripción aprobada espera 10 a 600 s entre reintentos (unos 150 s para cinco
        entregas); para que la prueba sea viable se crea aquí una suscripción equivalente
        (orden, DLQ y 5 entregas) con backoff de 1 s. Las políticas reales se verifican en
        `test_setup_creates_ordered_subscription_with_dlq`.
        """
        suffix = uuid.uuid4().hex[:6]
        topic = f"projects/{self.project}/topics/{USERS_TOPIC}"
        dlq_topic = f"projects/{self.project}/topics/dlq-{suffix}"
        publisher = pubsub_v1.PublisherClient()
        publisher.create_topic(name=dlq_topic)
        types = pubsub_v1.types
        subscription = f"proyector-dlq-test-{suffix}"
        dlq_listener = self.subscriber.subscription_path(self.project, f"dlq-ops-{suffix}")
        self.subscriber.create_subscription(request={"name": dlq_listener, "topic": dlq_topic})
        self.subscriber.create_subscription(
            request=types.Subscription(
                name=self.subscriber.subscription_path(self.project, subscription),
                topic=topic,
                enable_message_ordering=True,
                dead_letter_policy=types.DeadLetterPolicy(
                    dead_letter_topic=dlq_topic, max_delivery_attempts=MAX_DELIVERY_ATTEMPTS
                ),
                retry_policy=types.RetryPolicy(
                    minimum_backoff={"seconds": 1}, maximum_backoff={"seconds": 1}
                ),
            )
        )
        invalid = profile_envelope(ProfileSection.DEMOGRAPHIC, uuid.uuid4(), 1, schema_version=9)
        self.publish(invalid)
        attempts: list[int | None] = []
        projector = ProjectionMessageHandler(self.db.projector())

        def handler(message: Any) -> None:
            attempts.append(message.delivery_attempt)
            projector(message)

        dead: list[Any] = []

        def reached_dlq() -> bool:
            response = self.subscriber.pull(
                request={"subscription": dlq_listener, "max_messages": 10}, timeout=5
            )
            dead.extend(response.received_messages)
            return bool(dead)

        arrived = self.consume(handler, reached_dlq, subscription=subscription, timeout=90.0)

        self.assertTrue(arrived, "el mensaje inválido no llegó a la DLQ")
        self.assertEqual(json.loads(dead[0].message.data)["event_id"], invalid["event_id"])
        self.assertEqual(dead[0].message.attributes["event_type"], invalid["event_type"])
        self.assertGreaterEqual(len(attempts), MAX_DELIVERY_ATTEMPTS)
        self.assertEqual(self.db.count(ProcessedEventRow), 0)
        self.assertEqual(self.db.count(AthleteProfileViewRow), 0)

    def test_restart_after_commit_before_ack_does_not_duplicate_effects(self) -> None:
        """CMP-001 prueba 3: el proyector cae tras el COMMIT y antes del ACK; al reiniciar,
        la entrega repetida se reconoce como duplicada y se confirma."""
        # Dos usuarios (dos claves de orden): cada redelivery es independiente.
        registered_id, athlete_id = uuid.uuid4(), uuid.uuid4()
        self.publish(
            registered_envelope(registered_id),
            profile_envelope(ProfileSection.PHYSIOLOGICAL, athlete_id, 1),
        )
        project = self.db.projector()

        def crash_before_ack(message: Any) -> None:
            ProjectionMessageHandler(project)(_NoAck(message))
            message.nack()

        self.consume(crash_before_ack, lambda: self.db.count(ProcessedEventRow) == 2)
        outcomes: list[ProjectionOutcome] = []
        lock = threading.Lock()

        def recording(event: UsersEvent) -> ProjectionOutcome:
            outcome = project(event)
            with lock:
                outcomes.append(outcome)
            return outcome

        recovered = self.consume(
            ProjectionMessageHandler(recording),
            lambda: len(outcomes) >= 2,
            timeout=90.0,  # el mensaje rechazado vuelve tras la espera mínima de 10 s
        )

        self.assertTrue(recovered, "las entregas repetidas no llegaron tras reiniciar")
        self.assertEqual(outcomes, [ProjectionOutcome.DUPLICATE] * 2)
        self.assertEqual(self.db.count(ProcessedEventRow), 2)
        self.assertEqual(self.db.count(UserDirectoryRow), 1)
        view = self.db.reader().get(athlete_id)
        assert view is not None
        self.assertEqual(view.physiological_version, 1)


class _NoAck:
    """Mensaje cuyo ACK se pierde: simula una caída justo después del COMMIT."""

    def __init__(self, message: Any) -> None:
        self._message = message

    @property
    def data(self) -> bytes:
        return bytes(self._message.data)

    @property
    def message_id(self) -> str:
        return str(self._message.message_id)

    @property
    def delivery_attempt(self) -> int | None:
        attempt: int | None = self._message.delivery_attempt
        return attempt

    def ack(self) -> None:
        pass

    def nack(self) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
