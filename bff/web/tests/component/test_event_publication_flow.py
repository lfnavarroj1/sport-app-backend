"""Componente HU024 en web: BFF → Usuarios (sesión y JWKS) → Eventos → outbox → Pub/Sub →
`eventos-read-projector` → `event_read` `PUBLISHED`, visible en `GET /v1/events` (HU035).

Servicios reales con PostgreSQL migrado (requiere `TEST_DATABASE_URL`); el tramo de
mensajería usa el emulador de Pub/Sub (`PUBSUB_EMULATOR_HOST`) con un proyecto propio.
Afiliación se simula solo para la capacidad `calendar.sport.view` del deportista que
consulta el calendario (su contrato se prueba en su servicio).
"""

import contextlib
import json
import os
import time
import unittest
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from fastapi.testclient import TestClient
from google.cloud import pubsub_v1
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from bff_web.main import create_app as create_bff
from bff_web.settings import BffWebSettings
from events_management.application.projections.event_projection import EventsProjector
from events_management.infrastructure.clock import SystemClock
from events_management.infrastructure.messaging.pubsub import PubSubEventPublisher
from events_management.infrastructure.persistence_read.events import (
    SqlAlchemyProjectionUnitOfWork,
)
from events_management.infrastructure.persistence_read.models import (
    EventReadRow,
    ProcessedEventRow,
)
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
from users_management.application.messages import parse_users_event
from users_management.application.projections.users_projection import UsersProjector
from users_management.infrastructure.clock import SystemClock as UsersClock
from users_management.infrastructure.persistence_read.projection import (
    SqlAlchemyProjectionUnitOfWork as UsersProjectionUnitOfWork,
)
from users_management.infrastructure.persistence_write.outbox import (
    SqlAlchemyOutboxStore as UsersOutboxStore,
)
from users_management.main import create_app as create_users
from users_management.settings import UsersManagementSettings
from users_management.workers.outbox_publisher import build_envelope as users_envelope

USERS = "users_management.infrastructure.persistence_write:migrations"
USERS_READ = "users_management.infrastructure.persistence_read:migrations"
EVENTS_WRITE = "events_management.infrastructure.persistence_write:migrations"
EVENTS_READ = "events_management.infrastructure.persistence_read:migrations"
POLICIES = "2026-10"
PASSWORD = "contraseña-muy-secreta"
ORIGIN = "http://localhost:4200"
EVENT_REQUEST: dict[str, Any] = {
    "name": "Carrera 10K Bogotá",
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


def wait_until(condition: Callable[[], bool], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


def calendar_capability(request: httpx.Request) -> httpx.Response:
    """Afiliación simulada: todo deportista tiene `calendar.sport.view` (HU035)."""
    return httpx.Response(
        200, json={"subscription_status": "ACTIVE", "capabilities": ["calendar.sport.view"]}
    )


class EventFlowCase(unittest.TestCase):
    """Usuarios, Eventos (escritura y lectura) y BFF web reales; base de HU024 y HU023."""

    def setUp(self) -> None:
        users_url = self.enterContext(temporary_database("hu024_flow_users"))
        # HU022: Usuarios con su base de lectura migrada (`user_directory` para los nombres).
        users_read_url = self.enterContext(temporary_database("hu024_flow_users_r"))
        self.write_url = self.enterContext(temporary_database("hu024_flow_events_w"))
        read_url = self.enterContext(temporary_database("hu024_flow_events_r"))
        upgrade(USERS, users_url)
        upgrade(USERS_READ, users_read_url)
        upgrade(EVENTS_WRITE, self.write_url)
        upgrade(EVENTS_READ, read_url)
        users = self.enterContext(
            TestClient(
                create_users(
                    UsersManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_write_url=users_url,  # type: ignore[arg-type]
                        database_read_url=users_read_url,  # type: ignore[arg-type]
                        registration_policies_version=POLICIES,
                        health_data_consent_version="health-test.v1",
                        jwt_private_key=ed25519_private_key_pem(),  # type: ignore[arg-type]
                    )
                ),
                base_url="http://users-management",
            )
        )
        self.events = self.enterContext(
            TestClient(
                create_events(
                    EventsManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_write_url=self.write_url,  # type: ignore[arg-type]
                        database_read_url=read_url,  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                    ),
                    users_http=users,
                    affiliation_http=httpx.Client(
                        transport=httpx.MockTransport(calendar_capability),
                        base_url="http://affiliation-management",
                    ),
                ),
                base_url="http://events-management",
            )
        )
        self.users = users
        self.bff = self.bff_with(self.events)
        write_engine, read_engine = create_engine(self.write_url), create_engine(read_url)
        self.addCleanup(write_engine.dispose)
        self.addCleanup(read_engine.dispose)
        self.writes, self.reads = sessionmaker(write_engine), sessionmaker(read_engine)
        users_write_engine = create_engine(users_url)
        users_read_engine = create_engine(users_read_url)
        self.addCleanup(users_write_engine.dispose)
        self.addCleanup(users_read_engine.dispose)
        self.users_writes = sessionmaker(users_write_engine)
        self.users_reads = sessionmaker(users_read_engine)

    def bff_with(self, events_http: httpx.Client) -> TestClient:
        return self.enterContext(
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
                    users_http=self.users,
                    events_http=events_http,
                ),
                base_url="https://bff-web",
                headers={"Origin": ORIGIN},
            )
        )

    def session_for(self, actor_type: str, full_name: str = "Persona Sintética") -> dict[str, str]:
        email = f"{actor_type}-{uuid.uuid4().hex[:8]}@example.com"
        registered = self.bff.post(
            "/v1/users",
            json={
                "full_name": full_name,
                "email": email,
                "password": PASSWORD,
                "actor_type": actor_type,
                "accepted_policies_version": POLICIES,
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        login = self.bff.post("/v1/sessions", json={"email": email, "password": PASSWORD})
        self.last_user_id = str(registered.json()["user_id"])
        return {"Authorization": f"Bearer {login.json()['access_token']}"}

    def draft(self, organizer: dict[str, str], **overrides: Any) -> str:
        created = self.bff.post(
            "/v1/events",
            json={**EVENT_REQUEST, **overrides},
            headers={**organizer, "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(created.status_code, 201, created.text)
        event_id: str = created.json()["event_id"]
        return event_id

    def publish(
        self, user: dict[str, str], event_id: str, key: str | None = None
    ) -> httpx.Response:
        headers = {**user, **({"Idempotency-Key": key} if key else {})}
        return self.bff.post(f"/v1/events/{event_id}/publish", headers=headers)

    def project_users(self) -> None:
        """`usuarios-outbox-publisher` + `usuarios-read-projector` sin Pub/Sub (HU022: el
        directorio de nombres; su transporte por Pub/Sub lo cubre la integración de Usuarios)."""
        outbox = UsersOutboxStore(self.users_writes)
        project = UsersProjector(
            unit_of_work=lambda: UsersProjectionUnitOfWork(self.users_reads), clock=UsersClock()
        )
        for event in outbox.claim_batch("w-test", 100, 30):
            project(parse_users_event(json.loads(json.dumps(users_envelope(event)))))
            outbox.mark_published(event.event_id, "w-test")

    def count(self, sessions: sessionmaker[Any], model: Any) -> int:
        with sessions() as session:
            return session.scalar(select(func.count()).select_from(model)) or 0

    def project_through_pubsub(self, project_id: str, processed: int) -> None:
        """`eventos-outbox-publisher` y `eventos-read-projector` reales sobre el emulador."""
        publisher = PubSubEventPublisher(project_id, EVENTS_TOPIC)
        try:
            OutboxRelay(
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
        subscriber = pubsub_v1.SubscriberClient()
        self.addCleanup(subscriber.close)
        streaming = subscriber.subscribe(
            subscriber.subscription_path(project_id, PROJECTOR_SUBSCRIPTION),
            callback=ProjectionMessageHandler(
                EventsProjector(
                    unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(self.reads),
                    clock=SystemClock(),
                )
            ),
        )
        try:
            done = wait_until(lambda: self.count(self.reads, ProcessedEventRow) == processed)
        finally:
            streaming.cancel()
            with contextlib.suppress(Exception):
                streaming.result(timeout=10)
        self.assertTrue(done, f"el proyector no llegó a {processed} mensajes")


class EventPublicationFlowTest(EventFlowCase):
    @unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
    def test_publication_flow_through_the_web_channel(self) -> None:
        """CA1, CA3, CA4, CA5 y CA6 de punta a punta en el backend."""
        project_id = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        ensure_resources(project_id)
        organizer, stranger = self.session_for("organizer"), self.session_for("organizer")
        athlete = self.session_for("athlete")
        event_id = self.draft(organizer)
        self.project_through_pubsub(project_id, processed=1)

        # CA6 y CA3: el borrador aparece solo en la lista y el detalle de su organizador.
        drafts = self.bff.get("/v1/me/events", params={"status": "DRAFT"}, headers=organizer)
        self.assertEqual([i["event_id"] for i in drafts.json()["items"]], [event_id])
        self.assertEqual(self.bff.get("/v1/me/events", headers=stranger).json()["items"], [])
        own = self.bff.get(f"/v1/events/{event_id}", headers=organizer)
        self.assertEqual((own.status_code, own.json()["status"]), (200, "DRAFT"))
        self.assertNotIn("my_registration", own.json())
        for user in (stranger, athlete):
            hidden = self.bff.get(f"/v1/events/{event_id}", headers=user)
            self.assertEqual((hidden.status_code, hidden.json()["code"]), (404, "event_not_found"))
        foreign = self.publish(stranger, event_id, str(uuid.uuid4()))
        self.assertEqual((foreign.status_code, foreign.json()["code"]), (404, "event_not_found"))

        # CA1 y CA4: publicar, repetir la clave y publicar de nuevo con otra clave.
        key = str(uuid.uuid4())
        published = self.publish(organizer, event_id, key)
        replay = self.publish(organizer, event_id, key)
        again = self.publish(organizer, event_id, str(uuid.uuid4()))
        other = self.draft(organizer)
        reused = self.publish(organizer, other, key)

        self.assertEqual(published.status_code, 200, published.text)
        body = published.json()
        self.assertEqual((body["status"], body["version"]), ("PUBLISHED", 2))
        self.assertTrue(body["published_at"].endswith("Z"))
        self.assertEqual((replay.json(), replay.headers["Idempotent-Replayed"]), (body, "true"))
        self.assertEqual((again.status_code, again.json()), (200, body))
        self.assertEqual(
            (reused.status_code, reused.json()["code"]), (409, "idempotency_key_reused")
        )
        with self.writes() as session:
            outbox = session.scalars(
                select(OutboxEventRow.event_type).where(
                    OutboxEventRow.event_type == "evento.publicado.v1"
                )
            ).all()
        self.assertEqual(len(outbox), 1, "una sola transición y un solo evento")

        # CA5: la respuesta no esperó la lectura; tras proyectar, es pública.
        listed_before = self.bff.get(
            "/v1/me/events", params={"status": "PUBLISHED"}, headers=organizer
        )
        self.assertEqual(listed_before.json()["items"], [])
        self.project_through_pubsub(project_id, processed=3)  # dos creados y un publicado
        listed = self.bff.get("/v1/me/events", params={"status": "PUBLISHED"}, headers=organizer)
        self.assertEqual([i["event_id"] for i in listed.json()["items"]], [event_id])
        self.assertEqual(listed.json()["items"][0]["published_at"], body["published_at"])
        public = self.bff.get(f"/v1/events/{event_id}", headers=athlete)
        self.assertEqual((public.status_code, public.json()["status"]), (200, "PUBLISHED"))
        # Periodo de hasta 92 días (ADR-040) alrededor del evento del 2099-11-15.
        calendar = self.events.get(
            "/v1/events",
            params={"from": "2099-11-14T00:00:00Z", "to": "2099-11-17T00:00:00Z"},
            headers=athlete,
        )
        self.assertEqual(calendar.status_code, 200, calendar.text)
        self.assertEqual([i["event_id"] for i in calendar.json()["items"]], [event_id])
        with self.reads() as session:
            row = session.get(EventReadRow, uuid.UUID(event_id))
        assert row is not None
        self.assertEqual((row.status, row.version), ("PUBLISHED", 2))

    def test_requirements_and_actor_rejections_keep_the_draft(self) -> None:
        """CA2 y CA6: inicio vencido → `422 not_in_future`; otro actor → `403`; sin cambios."""
        organizer = self.session_for("organizer")
        event_id = self.draft(organizer)
        with self.writes() as session, session.begin():
            session.execute(
                text(
                    "UPDATE event SET starts_at = now() - interval '1 hour', "
                    "ends_at = now() - interval '1 hour' + make_interval(mins => duration_minutes) "
                    "WHERE event_id = :id"
                ),
                {"id": event_id},
            )

        late = self.publish(organizer, event_id, str(uuid.uuid4()))
        missing_key = self.publish(organizer, event_id)
        denied = [
            self.publish(self.session_for(actor), event_id, str(uuid.uuid4()))
            for actor in ("athlete", "service_provider")
        ]

        self.assertEqual((late.status_code, late.json()["code"]), (422, "validation_error"))
        self.assertEqual(late.json()["details"], [{"field": "starts_at", "issue": "not_in_future"}])
        self.assertEqual(
            missing_key.json()["details"], [{"field": "Idempotency-Key", "issue": "required"}]
        )
        for response in denied:
            self.assertEqual(
                (response.status_code, response.json()["code"]), (403, "permission_required")
            )
        with self.writes() as session:
            self.assertEqual(session.get(EventRow, uuid.UUID(event_id)).status, "DRAFT")  # type: ignore[union-attr]
            published = session.scalar(
                select(func.count())
                .select_from(OutboxEventRow)
                .where(OutboxEventRow.event_type == "evento.publicado.v1")
            )
        self.assertEqual(published, 0)


if __name__ == "__main__":
    unittest.main()
