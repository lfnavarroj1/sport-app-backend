"""Componente HU021 en web: BFF → Usuarios (sesión y JWKS) → Eventos → outbox → Pub/Sub
→ `eventos-read-projector` → `event_read`.

Servicios reales con PostgreSQL migrado (requiere `TEST_DATABASE_URL`). El tramo de
mensajería usa el emulador de Pub/Sub (`PUBSUB_EMULATOR_HOST`) con un proyecto propio.
"""

import contextlib
import os
import time
import unittest
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from fastapi.testclient import TestClient
from google.cloud import pubsub_v1
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from bff_web.main import create_app as create_bff
from bff_web.settings import BffWebSettings
from events_management.application.projections.event_projection import ProjectEventCreated
from events_management.infrastructure.clock import SystemClock
from events_management.infrastructure.messaging.pubsub import PubSubEventPublisher
from events_management.infrastructure.persistence_read.events import (
    SqlAlchemyEventReader,
    SqlAlchemyProjectionUnitOfWork,
)
from events_management.infrastructure.persistence_read.models import EventReadRow
from events_management.infrastructure.persistence_write.models import EventRow, OutboxEventRow
from events_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from events_management.main import create_app as create_events
from events_management.settings import (
    EVENTS_TOPIC,
    PROJECTOR_SUBSCRIPTION,
    EventsManagementSettings,
    OutboxPublisherSettings,
)
from events_management.workers.outbox_publisher import OutboxRelay
from events_management.workers.pubsub_setup import ensure_resources
from events_management.workers.read_projector import ProjectionMessageHandler
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from users_management.main import create_app as create_users
from users_management.settings import UsersManagementSettings

USERS = "users_management.infrastructure.persistence_write:migrations"
EVENTS_WRITE = "events_management.infrastructure.persistence_write:migrations"
EVENTS_READ = "events_management.infrastructure.persistence_read:migrations"
POLICIES = "2026-10"
PASSWORD = "contraseña-muy-secreta"
ORIGIN = "http://localhost:4200"


def event_request(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": "  Carrera 10K Bogotá  ",
        "description": "Carrera recreativa de diez kilómetros para corredores aficionados.",
        "discipline": "Running",
        "location": {
            "name": "Parque Metropolitano Simón Bolívar",
            "address": "Avenida Calle 63 # 60-50",
            "city": "Bogotá",
            "country_code": "CO",
        },
        "starts_at": "2099-11-15T07:00:00-05:00",
        "time_zone": "America/Bogota",
        "duration_minutes": 180,
        "capacity": 5000,
    }
    values.update(overrides)
    return values


def wait_until(condition: Callable[[], bool], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


class EventCreationFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        users_url = self.enterContext(temporary_database("hu021_flow_users"))
        self.write_url = self.enterContext(temporary_database("hu021_flow_events_w"))
        read_url = self.enterContext(temporary_database("hu021_flow_events_r"))
        upgrade(USERS, users_url)
        upgrade(EVENTS_WRITE, self.write_url)
        upgrade(EVENTS_READ, read_url)
        users = self.enterContext(
            TestClient(
                create_users(
                    UsersManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_write_url=users_url,  # type: ignore[arg-type]
                        database_read_url=users_url,  # type: ignore[arg-type]
                        registration_policies_version=POLICIES,
                        health_data_consent_version="health-test.v1",
                        jwt_private_key=ed25519_private_key_pem(),  # type: ignore[arg-type]
                    )
                ),
                base_url="http://users-management",
            )
        )
        events = self.enterContext(
            TestClient(
                create_events(
                    EventsManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_write_url=self.write_url,  # type: ignore[arg-type]
                        database_read_url=read_url,  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
                    ),
                    users_http=users,
                ),
                base_url="http://events-management",
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
                        complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
                        events_management_base_url="http://events-management",  # type: ignore[arg-type]
                        cors_allowed_origins=(ORIGIN,),
                    ),
                    users_http=users,
                    events_http=events,
                ),
                base_url="https://bff-web",
                headers={"Origin": ORIGIN},
            )
        )
        write_engine, read_engine = create_engine(self.write_url), create_engine(read_url)
        self.addCleanup(write_engine.dispose)
        self.addCleanup(read_engine.dispose)
        self.writes, self.reads = sessionmaker(write_engine), sessionmaker(read_engine)

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

    def create(self, user: dict[str, str], body: Any, key: str) -> httpx.Response:
        return self.bff.post(
            "/v1/events",
            json=body,
            headers={"Authorization": user["Authorization"], "Idempotency-Key": key},
        )

    def count(self, sessions: sessionmaker[Any], model: Any) -> int:
        with sessions() as session:
            return session.scalar(select(func.count()).select_from(model)) or 0

    def test_command_flow_through_the_web_channel(self) -> None:
        """CA1-CA4 por el canal web; la respuesta no espera la proyección (CA5)."""
        organizer = self.session_for("organizer")
        key = str(uuid.uuid4())

        created = self.create(organizer, event_request(), key)
        retried = self.create(organizer, event_request(), key)
        reused = self.create(organizer, event_request(capacity=10), key)

        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        self.assertEqual(body["organizer_id"], organizer["user_id"])  # del token
        self.assertEqual((body["status"], body["version"]), ("DRAFT", 1))
        self.assertEqual(body["name"], "Carrera 10K Bogotá")  # recortado
        self.assertEqual(
            (body["starts_at"], body["ends_at"], body["time_zone"]),
            ("2099-11-15T12:00:00Z", "2099-11-15T15:00:00Z", "America/Bogota"),
        )
        self.assertEqual(created.headers["Location"], f"/v1/events/{body['event_id']}")
        self.assertEqual(retried.json(), body)
        self.assertEqual(retried.headers["Idempotent-Replayed"], "true")
        self.assertEqual(
            (reused.status_code, reused.json()["code"]), (409, "idempotency_key_reused")
        )

        invalid = self.create(
            organizer,
            event_request(organizer_id=str(uuid.uuid4()), time_zone="Europe/Madrid"),
            str(uuid.uuid4()),
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertCountEqual(
            invalid.json()["details"],
            [
                {"field": "organizer_id", "issue": "field_not_allowed"},
                {"field": "time_zone", "issue": "time_zone_offset_mismatch"},
            ],
        )
        self.assertEqual(
            (self.count(self.writes, EventRow), self.count(self.writes, OutboxEventRow)), (1, 1)
        )
        self.assertEqual(self.count(self.reads, EventReadRow), 0)  # la lectura es asíncrona

    def test_other_actors_cannot_create(self) -> None:
        """CA2: el borde y el servicio rechazan a deportistas y prestadores sin persistir."""
        for actor in ("athlete", "service_provider"):
            with self.subTest(actor=actor):
                user = self.session_for(actor)
                response = self.create(user, event_request(), str(uuid.uuid4()))
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )
        self.assertEqual(self.count(self.writes, EventRow), 0)

    @unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
    def test_created_event_reaches_the_private_projection_through_pubsub(self) -> None:
        """CA5 y CA6: outbox → Pub/Sub → proyector; el borrador solo lo ve su organizador."""
        project_id = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        ensure_resources(project_id)
        organizer = self.session_for("organizer")
        body = self.create(organizer, event_request(), str(uuid.uuid4())).json()

        publisher = PubSubEventPublisher(project_id, EVENTS_TOPIC)
        try:
            published = OutboxRelay(
                SqlAlchemyOutboxStore(self.writes),
                publisher,
                OutboxPublisherSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_write_url=self.write_url,  # type: ignore[arg-type]
                    pubsub_project_id=project_id,
                    worker_id="w-test",
                ),
            ).run_once()
        finally:
            publisher.close()
        self.assertEqual(published, 1)

        subscriber = pubsub_v1.SubscriberClient()
        self.addCleanup(subscriber.close)
        streaming = subscriber.subscribe(
            subscriber.subscription_path(project_id, PROJECTOR_SUBSCRIPTION),
            callback=ProjectionMessageHandler(
                ProjectEventCreated(
                    unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(self.reads),
                    clock=SystemClock(),
                )
            ),
        )
        try:
            projected = wait_until(lambda: self.count(self.reads, EventReadRow) == 1)
        finally:
            streaming.cancel()
            with contextlib.suppress(Exception):
                streaming.result(timeout=10)

        self.assertTrue(projected, "el borrador no llegó a event_read")
        reader = SqlAlchemyEventReader(self.reads)
        event_id, organizer_id = uuid.UUID(body["event_id"]), uuid.UUID(organizer["user_id"])
        draft = reader.owned_by(organizer_id, event_id)
        assert draft is not None
        self.assertEqual((draft.status.value, draft.version), ("DRAFT", 1))
        self.assertIsNone(reader.owned_by(uuid.uuid4(), event_id))
        self.assertEqual(reader.public_events(), [])


if __name__ == "__main__":
    unittest.main()
