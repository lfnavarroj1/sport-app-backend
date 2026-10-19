"""`servicios-outbox-publisher` de HU019 con PostgreSQL real y, si está disponible, con el
emulador de Pub/Sub (ADR-027).

Requiere `TEST_DATABASE_URL`; el tramo de mensajería requiere además
`PUBSUB_EMULATOR_HOST` y usa un proyecto propio del emulador por prueba.
"""

import json
import os
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from google.cloud import pubsub_v1
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from complementary_services.infrastructure.messaging.pubsub import PubSubEventPublisher
from complementary_services.infrastructure.persistence.outbox import SqlAlchemyOutboxStore
from complementary_services.settings import (
    PUBLISHED_RETENTION,
    SERVICES_TOPIC,
    OutboxPublisherSettings,
)
from complementary_services.workers.outbox_publisher import OutboxRelay
from complementary_services.workers.pubsub_setup import ensure_resources
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.contracts import repository_root
from sportapp_platform.testing.postgres import temporary_database

HISTORY = "complementary_services.infrastructure.persistence:migrations"
SCHEMA = (
    repository_root(Path(__file__).resolve())
    / "contracts"
    / "events"
    / "servicios"
    / "servicio.publicado.v1.schema.json"
)

_INSERT = text(
    """
    INSERT INTO outbox_event (
        event_id, aggregate_type, aggregate_id, aggregate_version, event_type,
        schema_version, occurred_at, correlation_id, causation_id, payload,
        published_at, attempts, next_attempt_at
    ) VALUES (
        :event_id, 'service', :aggregate_id, 2, 'servicio.publicado.v1',
        1, :occurred_at, :correlation_id, :causation_id, CAST(:payload AS JSONB),
        :published_at, 0, :next_attempt_at
    )
    """
)


class OutboxStoreIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.url = self.enterContext(temporary_database("hu019_outbox"))
        upgrade(HISTORY, self.url)
        engine = create_engine(self.url)
        self.addCleanup(engine.dispose)
        self.sessions = sessionmaker(engine)
        self.store = SqlAlchemyOutboxStore(self.sessions)
        self.now = datetime.now(UTC)

    def insert(
        self,
        *,
        occurred_at: datetime | None = None,
        published_at: datetime | None = None,
        next_attempt_at: datetime | None = None,
    ) -> uuid.UUID:
        event_id, service_id = uuid.uuid4(), uuid.uuid4()
        occurred = occurred_at or self.now - timedelta(seconds=1)
        payload = {
            "service_id": str(service_id),
            "provider_id": str(uuid.uuid4()),
            "status": "PUBLISHED",
            "published_at": occurred.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
        with self.sessions() as session, session.begin():
            session.execute(
                _INSERT,
                {
                    "event_id": event_id,
                    "aggregate_id": service_id,
                    "occurred_at": occurred,
                    "correlation_id": uuid.uuid4(),
                    "causation_id": str(uuid.uuid4()),
                    "payload": json.dumps(payload),
                    "published_at": published_at,
                    "next_attempt_at": next_attempt_at or occurred,
                },
            )
        return event_id

    def row(self, event_id: uuid.UUID) -> dict[str, Any] | None:
        with self.sessions() as session:
            found = (
                session.execute(
                    text(
                        "SELECT published_at, attempts, next_attempt_at, lease_owner, last_error "
                        "FROM outbox_event WHERE event_id = :event_id"
                    ),
                    {"event_id": event_id},
                )
                .mappings()
                .first()
            )
        return dict(found) if found else None

    def test_claim_skips_locked_and_leased_events(self) -> None:
        """`FOR UPDATE SKIP LOCKED` + arrendamiento: dos réplicas no reclaman el mismo evento."""
        locked, free = self.insert(), self.insert()

        with self.sessions() as rival, rival.begin():
            rival.execute(
                text("SELECT event_id FROM outbox_event WHERE event_id = :id FOR UPDATE"),
                {"id": locked},
            )
            first = self.store.claim_batch("w-1", 10, 30)

        self.assertEqual([event.event_id for event in first], [free])
        self.assertEqual(first[0].aggregate_type, "service")
        self.assertEqual(first[0].payload["status"], "PUBLISHED")
        second = self.store.claim_batch("w-2", 10, 30)
        self.assertEqual([event.event_id for event in second], [locked], "el otro sigue arrendado")
        self.assertEqual(self.store.claim_batch("w-3", 10, 30), [])

    def test_crash_after_publishing_is_recovered_by_another_worker(self) -> None:
        """Si un worker publica y muere antes de marcar, al vencer el arrendamiento otro lo
        reclama y lo marca: entrega al menos una vez, nunca se pierde."""
        event_id = self.insert()
        [crashed] = self.store.claim_batch("w-1", 10, 30)
        self.assertEqual(self.store.claim_batch("w-2", 10, 30), [], "aún arrendado")
        with self.sessions() as session, session.begin():
            session.execute(
                text(
                    "UPDATE outbox_event SET lease_until = now() - interval '1 second' "
                    "WHERE event_id = :event_id"
                ),
                {"event_id": event_id},
            )

        [recovered] = self.store.claim_batch("w-2", 10, 30)
        self.store.mark_published(crashed.event_id, "w-1")  # el worker caído ya no es dueño
        self.assertIsNone(self.row(event_id)["published_at"])  # type: ignore[index]
        self.store.mark_published(recovered.event_id, "w-2")

        self.assertEqual(recovered.event_id, crashed.event_id)
        self.assertIsNotNone(self.row(event_id)["published_at"])  # type: ignore[index]
        self.assertEqual(self.store.claim_batch("w-3", 10, 30), [])

    def test_mark_published_only_by_the_lease_owner(self) -> None:
        event_id = self.insert()
        [claimed] = self.store.claim_batch("w-1", 10, 30)

        self.store.mark_published(claimed.event_id, "otro-worker")
        self.assertIsNone(self.row(event_id)["published_at"])  # type: ignore[index]

        self.store.mark_published(claimed.event_id, "w-1")
        row = self.row(event_id)
        assert row is not None
        self.assertIsNotNone(row["published_at"])
        self.assertIsNone(row["lease_owner"])
        self.assertEqual(self.store.claim_batch("w-1", 10, 30), [])

    def test_failure_reschedules_without_discarding(self) -> None:
        event_id = self.insert()
        self.store.claim_batch("w-1", 10, 30)

        self.store.mark_failed(event_id, "w-1", "RuntimeError", 600)

        row = self.row(event_id)
        assert row is not None
        self.assertEqual((row["attempts"], row["last_error"]), (1, "RuntimeError"))
        self.assertIsNone(row["published_at"])
        self.assertGreater(row["next_attempt_at"], self.now + timedelta(seconds=590))
        self.assertEqual(self.store.claim_batch("w-1", 10, 30), [], "espera su turno")

    def test_purge_removes_only_old_published_events(self) -> None:
        """ADR-027: publicados retenidos siete días; un pendiente nunca se elimina."""
        expired = self.insert(published_at=self.now - timedelta(days=8))
        recent = self.insert(published_at=self.now - timedelta(days=6))
        old_pending = self.insert(occurred_at=self.now - timedelta(days=30))

        purged = self.store.purge_published(self.now - PUBLISHED_RETENTION)

        self.assertEqual(purged, 1)
        self.assertIsNone(self.row(expired))
        self.assertIsNotNone(self.row(recent))
        self.assertIsNotNone(self.row(old_pending))
        self.assertEqual(self.store.purge_published(self.now - PUBLISHED_RETENTION), 0)

    @unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
    def test_relay_publishes_a_valid_envelope_through_the_emulator(self) -> None:
        project = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        created = ensure_resources(project)
        self.assertEqual(created, [f"projects/{project}/topics/{SERVICES_TOPIC}"])
        self.assertEqual(ensure_resources(project), created, "repetirlo es inofensivo")
        subscriber = pubsub_v1.SubscriberClient()
        self.addCleanup(subscriber.close)
        listener = subscriber.subscription_path(project, f"test-{uuid.uuid4().hex[:6]}")
        subscriber.create_subscription(
            request={"name": listener, "topic": created[0], "enable_message_ordering": True}
        )
        event_id = self.insert()

        publisher = PubSubEventPublisher(project, SERVICES_TOPIC)
        try:
            published = OutboxRelay(
                self.store,
                publisher,
                OutboxPublisherSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_url=self.url,  # type: ignore[arg-type]
                    pubsub_project_id=project,
                    worker_id="w-test",
                ),
            ).run_once()
        finally:
            publisher.close()

        self.assertEqual(published, 1)
        received = subscriber.pull(
            request={"subscription": listener, "max_messages": 10}, timeout=30
        ).received_messages
        message = received[0].message
        body = json.loads(message.data)
        Draft202012Validator(
            json.loads(SCHEMA.read_text(encoding="utf-8")), format_checker=FormatChecker()
        ).validate(body)
        self.assertEqual(body["event_id"], str(event_id))
        self.assertEqual(message.ordering_key, body["aggregate_id"])
        self.assertEqual(message.attributes["event_type"], "servicio.publicado.v1")
        self.assertIsNotNone(self.row(event_id)["published_at"])  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
