"""Componente HU041 en móvil: BFF → Usuarios (JWKS) → Eventos (`POST /v1/events/{id}/registrations`,
que vuelve a verificar el actor) → PostgreSQL migrado → outbox → proyector real → «mis
inscripciones» y detalle (requiere `TEST_DATABASE_URL`). La variante con mensajería usa el
emulador de Pub/Sub (`PUBSUB_EMULATOR_HOST`) con un proyecto propio.

La publicación de eventos pertenece a HU024 (todavía inexistente): el evento `PUBLISHED` se
inserta de forma sintética en `event` y en `event_read`.
"""

import contextlib
import os
import time
import unittest
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi.testclient import TestClient
from google.cloud import pubsub_v1
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from bff_mobile.main import create_app as create_bff
from bff_mobile.settings import BffMobileSettings
from events_management.application.messages import parse_sport_event_message
from events_management.application.projections.event_projection import EventsProjector
from events_management.infrastructure.clock import SystemClock
from events_management.infrastructure.messaging.pubsub import PubSubEventPublisher
from events_management.infrastructure.persistence_read.events import (
    SqlAlchemyProjectionUnitOfWork,
)
from events_management.infrastructure.persistence_read.models import (
    EventReadRow,
    EventRegistrationReadRow,
)
from events_management.infrastructure.persistence_write.models import (
    EventRegistrationRow,
    EventRow,
    OutboxEventRow,
)
from events_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from events_management.main import create_app as create_events
from events_management.settings import (
    EVENTS_TOPIC,
    PROJECTOR_SUBSCRIPTION,
    EventsManagementSettings,
    OutboxPublisherSettings,
)
from events_management.workers.outbox_publisher import OutboxRelay, build_envelope
from events_management.workers.pubsub_setup import ensure_resources
from events_management.workers.read_projector import ProjectionMessageHandler
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from users_management.main import create_app as create_users
from users_management.settings import UsersManagementSettings

HISTORIES = {
    "users_w": "users_management.infrastructure.persistence_write:migrations",
    "users_r": "users_management.infrastructure.persistence_read:migrations",
    "events_w": "events_management.infrastructure.persistence_write:migrations",
    "events_r": "events_management.infrastructure.persistence_read:migrations",
}
POLICIES = "2026-10"
PASSWORD = "contraseña-muy-secreta"


def unused(_: httpx.Request) -> httpx.Response:
    raise AssertionError("HU041 no consulta este servicio")


def wait_until(condition: Callable[[], bool], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


class EventRegistrationFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.urls = {
            name: self.enterContext(temporary_database(f"hu041_{name}")) for name in HISTORIES
        }
        for name, history in HISTORIES.items():
            upgrade(history, self.urls[name])
        self.engines = {name: create_engine(url) for name, url in self.urls.items()}
        for engine in self.engines.values():
            self.addCleanup(engine.dispose)
        self.users = self.client(
            create_users(
                UsersManagementSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_write_url=self.urls["users_w"],  # type: ignore[arg-type]
                    database_read_url=self.urls["users_r"],  # type: ignore[arg-type]
                    registration_policies_version=POLICIES,
                    health_data_consent_version="health-test.v1",
                    jwt_private_key=ed25519_private_key_pem(),  # type: ignore[arg-type]
                )
            ),
            "http://users-management",
        )
        unused_client = httpx.Client(
            base_url="http://unused", transport=httpx.MockTransport(unused)
        )
        self.events = self.client(
            create_events(
                EventsManagementSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_write_url=self.urls["events_w"],  # type: ignore[arg-type]
                    database_read_url=self.urls["events_r"],  # type: ignore[arg-type]
                    users_management_base_url="http://users-management",  # type: ignore[arg-type]
                    affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                ),
                users_http=self.users,
                affiliation_http=unused_client,
            ),
            "http://events-management",
        )
        self.bff = self.client(
            create_bff(
                BffMobileSettings(
                    app_env="test",  # type: ignore[arg-type]
                    users_management_base_url="http://users-management",  # type: ignore[arg-type]
                    affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                    training_management_base_url="http://training-management",  # type: ignore[arg-type]
                    events_management_base_url="http://events-management",  # type: ignore[arg-type]
                    complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
                ),
                users_http=self.users,
                affiliation_http=unused_client,
                training_http=unused_client,
                events_http=self.events,
            ),
            "https://bff-mobile",
        )

    def client(self, app: Any, base_url: str) -> TestClient:
        return self.enterContext(TestClient(app, base_url=base_url))

    def sign_up(self, actor_type: str) -> dict[str, str]:
        email = f"{actor_type}-{uuid.uuid4().hex[:8]}@example.com"
        registered = self.users.post(
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
        login = self.users.post("/v1/sessions", json={"email": email, "password": PASSWORD})
        return {"Authorization": f"Bearer {login.json()['access_token']}"}

    def add_event(self, *, capacity: int = 100, status: str = "PUBLISHED") -> str:
        event_id, now = uuid.uuid4(), datetime.now(UTC)
        starts_at = now + timedelta(days=30)
        values: dict[str, Any] = {
            "event_id": event_id,
            "organizer_id": uuid.uuid4(),
            "name": "Carrera 10K Bogotá",
            "description": "Carrera recreativa de diez kilómetros para aficionados.",
            "discipline": "Running",
            "location_name": "Parque Metropolitano Simón Bolívar",
            "location_address": "Avenida Calle 63 # 60-50",
            "location_city": "Bogotá",
            "location_country_code": "CO",
            "starts_at": starts_at,
            "ends_at": starts_at + timedelta(hours=3),
            "time_zone": "America/Bogota",
            "duration_minutes": 180,
            "capacity": capacity,
            "status": status,
            "version": 2 if status == "PUBLISHED" else 1,
            "created_at": now,
            "updated_at": now,
            # Restricción de HU024: `published_at` existe si y solo si está publicado.
            "published_at": now if status == "PUBLISHED" else None,
        }
        with sessionmaker(self.engines["events_w"])() as session, session.begin():
            session.add(EventRow(**values))
        with sessionmaker(self.engines["events_r"])() as session, session.begin():
            session.add(EventReadRow(**values, projected_at=now))
        return str(event_id)

    def enroll(self, user: dict[str, str], event_id: str, key: str | None = None) -> httpx.Response:
        return self.bff.post(
            f"/v1/events/{event_id}/registrations",
            headers={**user, "Idempotency-Key": key or str(uuid.uuid4())},
        )

    def project_outbox(self) -> None:
        store = SqlAlchemyOutboxStore(sessionmaker(self.engines["events_w"]))
        project = EventsProjector(
            unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(
                sessionmaker(self.engines["events_r"])
            ),
            clock=SystemClock(),
        )
        for event in store.claim_batch("w-test", 100, 30):
            project(parse_sport_event_message(build_envelope(event)))
            store.mark_published(event.event_id, "w-test")

    def count(self, row: Any, database: str = "events_w") -> int:
        with sessionmaker(self.engines[database])() as session:
            return int(session.scalar(select(func.count()).select_from(row)) or 0)

    def test_athlete_registers_retries_and_sees_the_registration(self) -> None:
        """CA1, CA4, CA6, CA7 y CA8 de punta a punta."""
        athlete = self.sign_up("athlete")
        event_id, key = self.add_event(), str(uuid.uuid4())

        created = self.enroll(athlete, event_id, key)
        replayed = self.enroll(athlete, event_id, key)
        duplicate = self.enroll(athlete, event_id)

        self.assertEqual(created.status_code, 201, created.text)
        registration_id = created.json()["registration_id"]
        self.assertEqual(
            created.headers["Location"], f"/v1/me/event-registrations/{registration_id}"
        )
        self.assertEqual(
            (created.json()["status"], created.json()["applied_fee"]), ("REGISTERED", None)
        )
        self.assertEqual(replayed.status_code, 201)
        self.assertEqual(replayed.headers["Idempotent-Replayed"], "true")
        self.assertEqual(replayed.json()["registration_id"], registration_id)
        self.assertEqual(
            (duplicate.status_code, duplicate.json()["code"], duplicate.json()["details"]),
            (
                409,
                "event_registration_exists",
                [{"field": "registration_id", "issue": registration_id}],
            ),
        )
        self.assertEqual((self.count(EventRegistrationRow), self.count(OutboxEventRow)), (1, 1))

        self.project_outbox()
        listed = self.bff.get("/v1/me/event-registrations", headers=athlete)
        detail = self.bff.get(f"/v1/events/{event_id}", headers=athlete)

        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(
            [item["registration_id"] for item in listed.json()["items"]], [registration_id]
        )
        self.assertEqual(listed.json()["items"][0]["event"]["name"], "Carrera 10K Bogotá")
        self.assertEqual(detail.json()["my_registration"]["registration_id"], registration_id)
        self.assertIsNone(detail.json()["fee"])

    def test_rules_and_actor_are_enforced(self) -> None:
        """CA2, CA3, CA5 y actor no deportista."""
        first, second = self.sign_up("athlete"), self.sign_up("athlete")
        organizer = self.sign_up("organizer")
        single_seat, draft = self.add_event(capacity=1), self.add_event(status="DRAFT")

        self.assertEqual(self.enroll(first, single_seat).status_code, 201)
        full = self.enroll(second, single_seat)
        hidden = self.enroll(first, draft)
        forbidden = self.enroll(organizer, single_seat)
        missing_key = self.bff.post(f"/v1/events/{single_seat}/registrations", headers=second)

        self.assertEqual((full.status_code, full.json()["code"]), (409, "event_full"))
        self.assertEqual((hidden.status_code, hidden.json()["code"]), (404, "event_not_found"))
        self.assertEqual(
            (forbidden.status_code, forbidden.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(missing_key.status_code, 422)
        self.assertEqual(self.bff.get(f"/v1/events/{draft}", headers=first).status_code, 404)

    @unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
    def test_registration_reaches_my_registrations_through_pubsub(self) -> None:
        """CA7: outbox → `sportapp.eventos.v1` → `eventos-read-projector` → «mis inscripciones»."""
        project_id = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        ensure_resources(project_id)
        athlete = self.sign_up("athlete")
        created = self.enroll(athlete, self.add_event())
        self.assertEqual(created.status_code, 201, created.text)

        publisher = PubSubEventPublisher(project_id, EVENTS_TOPIC)
        try:
            published = OutboxRelay(
                SqlAlchemyOutboxStore(sessionmaker(self.engines["events_w"])),
                publisher,
                OutboxPublisherSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_write_url=self.urls["events_w"],  # type: ignore[arg-type]
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
                EventsProjector(
                    unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(
                        sessionmaker(self.engines["events_r"])
                    ),
                    clock=SystemClock(),
                )
            ),
        )
        try:
            projected = wait_until(lambda: self.count(EventRegistrationReadRow, "events_r") == 1)
        finally:
            streaming.cancel()
            with contextlib.suppress(Exception):
                streaming.result(timeout=10)

        self.assertTrue(projected, "la inscripción no llegó a event_registration_read")
        listed = self.bff.get("/v1/me/event-registrations", headers=athlete)
        self.assertEqual(
            [item["registration_id"] for item in listed.json()["items"]],
            [created.json()["registration_id"]],
        )


if __name__ == "__main__":
    unittest.main()
