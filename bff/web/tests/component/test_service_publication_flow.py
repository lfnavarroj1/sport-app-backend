"""Componente HU019 en web: BFF → Usuarios (sesión y JWKS) → Servicios → PostgreSQL →
outbox → `servicios-outbox-publisher` → `sportapp.servicios.v1`.

Servicios reales con PostgreSQL migrado (requiere `TEST_DATABASE_URL`). El tramo de
mensajería usa el emulador de Pub/Sub (`PUBSUB_EMULATOR_HOST`) con un proyecto propio y
una suscripción temporal creada por la prueba: HU019 no tiene consumidores aprobados.
"""

import json
import os
import time
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
from google.cloud import pubsub_v1
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from bff_web.main import create_app as create_bff
from bff_web.settings import BffWebSettings
from complementary_services.infrastructure.messaging.pubsub import PubSubEventPublisher
from complementary_services.infrastructure.persistence.models import OutboxEventRow, ServiceRow
from complementary_services.infrastructure.persistence.outbox import SqlAlchemyOutboxStore
from complementary_services.main import create_app as create_services
from complementary_services.settings import (
    SERVICES_TOPIC,
    ComplementaryServicesSettings,
    OutboxPublisherSettings,
)
from complementary_services.workers.outbox_publisher import OutboxRelay
from complementary_services.workers.pubsub_setup import ensure_resources
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.contracts import repository_root
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from users_management.main import create_app as create_users
from users_management.settings import UsersManagementSettings

USERS = "users_management.infrastructure.persistence_write:migrations"
SERVICES = "complementary_services.infrastructure.persistence:migrations"
POLICIES = "2026-10"
PASSWORD = "contraseña-muy-secreta"
ORIGIN = "http://localhost:4200"
SCHEMA = (
    repository_root(Path(__file__).resolve())
    / "contracts"
    / "events"
    / "servicios"
    / "servicio.publicado.v1.schema.json"
)


def offer(**overrides: Any) -> dict[str, Any]:
    day = (datetime.now(UTC) + timedelta(days=10)).date().isoformat()
    values: dict[str, Any] = {
        "name": "Valoración deportiva inicial",
        "description": "Valoración presencial para orientar el plan deportivo.",
        "category": "SPORTS_MEDICINE",
        "modality": "IN_PERSON",
        "price": {"amount": "120000.00", "currency": "COP", "billing_unit": "SESSION"},
        "availability": [
            {
                "starts_at": f"{day}T09:00:00-05:00",
                "ends_at": f"{day}T10:00:00-05:00",
                "time_zone": "America/Bogota",
                "capacity": 1,
            }
        ],
    }
    values.update(overrides)
    return values


class ServicePublicationFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        users_url = self.enterContext(temporary_database("hu019_flow_users"))
        self.services_url = self.enterContext(temporary_database("hu019_flow_services"))
        upgrade(USERS, users_url)
        upgrade(SERVICES, self.services_url)
        users = self.enterContext(
            TestClient(
                create_users(
                    UsersManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_write_url=users_url,  # type: ignore[arg-type]
                        database_read_url=users_url,  # type: ignore[arg-type]
                        registration_policies_version=POLICIES,
                        jwt_private_key=ed25519_private_key_pem(),  # type: ignore[arg-type]
                    )
                ),
                base_url="http://users-management",
            )
        )
        services = self.enterContext(
            TestClient(
                create_services(
                    ComplementaryServicesSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_url=self.services_url,  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                    ),
                    users_http=users,
                ),
                base_url="http://complementary-services",
            )
        )
        self.bff = self.enterContext(
            TestClient(
                create_bff(
                    BffWebSettings(
                        app_env="test",  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
                        training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
                        complementary_services_base_url="http://complementary-services",  # type: ignore[arg-type]
                        events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                        cors_allowed_origins=(ORIGIN,),
                    ),
                    users_http=users,
                    services_http=services,
                ),
                base_url="https://bff-web",
                headers={"Origin": ORIGIN},
            )
        )
        engine = create_engine(self.services_url)
        self.addCleanup(engine.dispose)
        self.sessions = sessionmaker(engine)

    def session_for(self, actor_type: str) -> dict[str, str]:
        email = f"{actor_type}-{uuid.uuid4().hex[:8]}@example.com"
        registered = self.bff.post(
            "/v1/users",
            json={
                "full_name": "Persona Sintética",
                "email": email,
                "password": PASSWORD,
                "actor_type": actor_type,
                "accepted_policies_version": POLICIES,
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        login = self.bff.post("/v1/sessions", json={"email": email, "password": PASSWORD})
        return {
            "user_id": registered.json()["user_id"],
            "Authorization": f"Bearer {login.json()['access_token']}",
        }

    def create(self, user: dict[str, str]) -> dict[str, Any]:
        created = self.bff.post(
            "/v1/services",
            json=offer(),
            headers={"Authorization": user["Authorization"], "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(created.status_code, 201, created.text)
        body: dict[str, Any] = created.json()
        return body

    def publish(
        self, user: dict[str, str], service_id: str, key: str, **headers: str
    ) -> httpx.Response:
        return self.bff.post(
            f"/v1/services/{service_id}/publish",
            headers={"Authorization": user["Authorization"], "Idempotency-Key": key, **headers},
        )

    def count(self, model: Any) -> int:
        with self.sessions() as session:
            return session.scalar(select(func.count()).select_from(model)) or 0

    def test_hu019_vertical_backend_flow(self) -> None:
        """Publicar por el canal web: transición, reintento idempotente, propiedad y lista
        propia; un solo evento en la outbox."""
        provider = self.session_for("service_provider")
        draft = self.create(provider)
        self.assertIsNone(draft["published_at"])
        key = str(uuid.uuid4())

        published = self.publish(provider, draft["service_id"], key)
        retried = self.publish(provider, draft["service_id"], key)

        self.assertEqual(published.status_code, 200, published.text)
        body = published.json()
        self.assertEqual(
            set(body),
            {"service_id", "provider_id", "status", "version", "published_at", "updated_at"},
        )
        self.assertEqual(body["service_id"], draft["service_id"])
        self.assertEqual(body["provider_id"], provider["user_id"])
        self.assertEqual((body["status"], body["version"]), ("PUBLISHED", 2))
        self.assertTrue(body["published_at"].endswith("Z"))
        self.assertTrue(body["updated_at"].endswith("Z"))
        self.assertEqual((retried.status_code, retried.json()), (200, body))
        self.assertEqual(retried.headers["Idempotent-Replayed"], "true")

        again = self.publish(provider, draft["service_id"], str(uuid.uuid4()))
        self.assertEqual((again.status_code, again.json()["version"]), (200, 2))  # sin evento

        stranger = self.session_for("service_provider")
        foreign = self.publish(stranger, draft["service_id"], str(uuid.uuid4()))
        self.assertEqual((foreign.status_code, foreign.json()["code"]), (404, "service_not_found"))

        auth = {"Authorization": provider["Authorization"]}
        listed = self.bff.get("/v1/me/services", params={"status": "PUBLISHED"}, headers=auth)
        self.assertEqual(listed.status_code, 200, listed.text)
        [item] = listed.json()["items"]
        self.assertEqual(
            (item["service_id"], item["status"], item["published_at"]),
            (draft["service_id"], "PUBLISHED", body["published_at"]),
        )
        self.assertEqual(
            self.bff.get("/v1/me/services", params={"status": "DRAFT"}, headers=auth).json()[
                "items"
            ],
            [],
        )
        detail = self.bff.get(f"/v1/me/services/{draft['service_id']}", headers=auth)
        self.assertEqual((detail.status_code, detail.json()["version"]), (200, 2))
        hidden = self.bff.get(
            f"/v1/me/services/{draft['service_id']}",
            headers={"Authorization": stranger["Authorization"]},
        )
        self.assertEqual((hidden.status_code, hidden.json()["code"]), (404, "service_not_found"))

        with self.sessions() as session:
            row = session.get(ServiceRow, uuid.UUID(draft["service_id"]))
        assert row is not None
        self.assertEqual((row.status, row.version), ("PUBLISHED", 2))
        self.assertEqual(self.count(OutboxEventRow), 1)

    def test_other_actors_cannot_publish_or_list(self) -> None:
        """El filtro de borde y el servicio rechazan a deportistas y organizadores."""
        provider = self.session_for("service_provider")
        draft = self.create(provider)
        for actor in ("athlete", "organizer"):
            with self.subTest(actor=actor):
                user = self.session_for(actor)
                published = self.publish(user, draft["service_id"], str(uuid.uuid4()))
                listed = self.bff.get(
                    "/v1/me/services", headers={"Authorization": user["Authorization"]}
                )
                self.assertEqual(
                    (published.status_code, published.json()["code"]),
                    (403, "permission_required"),
                )
                self.assertEqual(
                    (listed.status_code, listed.json()["code"]), (403, "permission_required")
                )
        self.assertEqual(self.count(OutboxEventRow), 0)

    @unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
    def test_published_service_reaches_the_topic_through_the_outbox(self) -> None:
        """outbox → `servicios-outbox-publisher` → `sportapp.servicios.v1`: sobre válido
        contra el esquema, `service_id` como clave de orden y la clave como causa."""
        project_id = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        [topic] = ensure_resources(project_id)
        subscriber = pubsub_v1.SubscriberClient()
        self.addCleanup(subscriber.close)
        listener = subscriber.subscription_path(project_id, f"test-{uuid.uuid4().hex[:6]}")
        subscriber.create_subscription(
            request={"name": listener, "topic": topic, "enable_message_ordering": True}
        )
        provider = self.session_for("service_provider")
        draft = self.create(provider)
        key, correlation = str(uuid.uuid4()), str(uuid.uuid4())
        published_now = self.publish(
            provider, draft["service_id"], key, **{"X-Correlation-ID": correlation}
        )
        self.assertEqual(published_now.status_code, 200)

        publisher = PubSubEventPublisher(project_id, SERVICES_TOPIC)
        try:
            published = OutboxRelay(
                SqlAlchemyOutboxStore(self.sessions),
                publisher,
                OutboxPublisherSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_url=self.services_url,  # type: ignore[arg-type]
                    pubsub_project_id=project_id,
                    worker_id="w-test",
                ),
            ).run_once()
        finally:
            publisher.close()
        self.assertEqual(published, 1)

        received: list[Any] = []
        deadline = time.monotonic() + 30
        while not received and time.monotonic() < deadline:
            # El emulador puede devolver un `pull` vacío aunque el mensaje ya esté publicado.
            received = list(
                subscriber.pull(
                    request={"subscription": listener, "max_messages": 10}, timeout=10
                ).received_messages
            )
        self.assertEqual(len(received), 1)
        message = received[0].message
        envelope = json.loads(message.data)
        Draft202012Validator(
            json.loads(SCHEMA.read_text(encoding="utf-8")), format_checker=FormatChecker()
        ).validate(envelope)
        self.assertEqual(message.ordering_key, draft["service_id"])
        self.assertEqual(message.attributes["event_type"], "servicio.publicado.v1")
        self.assertEqual(
            (envelope["aggregate_id"], envelope["aggregate_version"], envelope["causation_id"]),
            (draft["service_id"], 2, key),
        )
        self.assertEqual(envelope["correlation_id"], correlation, "la correlación llega al evento")
        self.assertEqual(
            envelope["payload"],
            {
                "service_id": draft["service_id"],
                "provider_id": provider["user_id"],
                "status": "PUBLISHED",
                "published_at": envelope["payload"]["published_at"],
            },
        )
        with self.sessions() as session:
            pending = session.scalar(
                select(func.count())
                .select_from(OutboxEventRow)
                .where(OutboxEventRow.published_at.is_(None))
            )
        self.assertEqual(pending, 0, "marcado publicado solo tras el ACK")


if __name__ == "__main__":
    unittest.main()
