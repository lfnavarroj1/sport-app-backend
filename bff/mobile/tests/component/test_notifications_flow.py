"""Componente HU039 (ADR-045): organizador publica en `events-management` → outbox →
`eventos-notificaciones-worker` (busca destinatarios en Usuarios con
`POST /v1/users/athletes/search`, real) → outbox → `eventos-read-projector` →
`GET /v1/me/notifications` por `bff-mobile`; y el planificador de recordatorios para un
inscrito. Todo con PostgreSQL migrado (requiere `TEST_DATABASE_URL`); la mensajería se recorre
sin Pub/Sub con los mismos sobres que publica el outbox.

Mismo montaje que `test_event_registration_flow.py` (se importa el módulo, no la clase, para no
volver a descubrir sus pruebas). Los perfiles (HU005 y HU006) se registran con las operaciones
reales de Usuarios y se proyectan como en HU027.
"""

import json
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import test_event_registration_flow as registration_flow
import test_event_suggestions_flow as suggestions_flow
from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from events_management.application.commands.generate_notifications import (
    GenerateEventMatchNotifications,
    GenerateEventReminders,
)
from events_management.application.messages import parse_sport_event_message
from events_management.application.projections.event_projection import EventsProjector
from events_management.domain.notifications.disciplines import load_aliases
from events_management.infrastructure.clock import SystemClock
from events_management.infrastructure.integrations.users import UsersAthleteDirectory
from events_management.infrastructure.persistence_read.events import (
    SqlAlchemyProjectionUnitOfWork,
)
from events_management.infrastructure.persistence_write.models import EventRow, NotificationRow
from events_management.infrastructure.persistence_write.notifications import (
    SqlAlchemyNotificationUnitOfWork,
)
from events_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from events_management.workers.notifications_worker import NotificationMessageHandler
from events_management.workers.outbox_publisher import build_envelope

INBOX = "/v1/me/notifications"
PREFERENCES = "/v1/me/notification-preferences"


class Delivery:
    """Mensaje de Pub/Sub mínimo para el worker."""

    def __init__(self, envelope: dict[str, Any]) -> None:
        self.data = json.dumps(envelope).encode("utf-8")
        self.message_id = envelope["event_id"]
        self.delivery_attempt = 1
        self.acked = self.nacked = False

    def ack(self) -> None:
        self.acked = True

    def nack(self) -> None:
        self.nacked = True


class NotificationsFlowTest(unittest.TestCase):
    sign_up = registration_flow.EventRegistrationFlowTest.sign_up
    client = registration_flow.EventRegistrationFlowTest.client
    add_event = registration_flow.EventRegistrationFlowTest.add_event
    enroll = registration_flow.EventRegistrationFlowTest.enroll
    count = registration_flow.EventRegistrationFlowTest.count
    profile = suggestions_flow.EventSuggestionsFlowTest.profile
    project_users = suggestions_flow.EventSuggestionsFlowTest.project_users

    def setUp(self) -> None:
        registration_flow.EventRegistrationFlowTest.setUp(self)  # type: ignore[arg-type]
        self.writes = sessionmaker(self.engines["events_w"])
        self.handler = NotificationMessageHandler(
            GenerateEventMatchNotifications(
                unit_of_work=lambda: SqlAlchemyNotificationUnitOfWork(self.writes),
                directory=UsersAthleteDirectory(self.users),
                aliases=load_aliases(),
                clock=SystemClock(),
            )
        )
        self.project = EventsProjector(
            unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(
                sessionmaker(self.engines["events_r"])
            ),
            clock=SystemClock(),
        )
        self.published: list[dict[str, Any]] = []

    def relay(self) -> None:
        """Outbox → proyector y, para `evento.publicado.v1`, también → worker de notificaciones
        (suscripciones distintas del mismo tópico), hasta vaciar la outbox."""
        store = SqlAlchemyOutboxStore(self.writes)
        while batch := store.claim_batch("w-test", 500, 30):
            for event in batch:
                envelope = json.loads(json.dumps(build_envelope(event)))
                self.project(parse_sport_event_message(envelope))
                if envelope["event_type"] == "evento.publicado.v1":
                    self.published.append(envelope)
                    delivery = Delivery(envelope)
                    self.handler(delivery)
                    self.assertTrue(delivery.acked)
                store.mark_published(event.event_id, "w-test")

    def publish_event(self, organizer: dict[str, str], *, discipline: str, city: str) -> str:
        body = {
            "name": f"{discipline} {city} 2099",
            "description": "Evento sintético para las notificaciones de HU039.",
            "discipline": discipline,
            "location": {
                "name": "Parque principal",
                "address": "Avenida Principal # 1-23",
                "city": city,
                "country_code": "CO",
            },
            "starts_at": "2099-11-15T07:00:00-05:00",
            "time_zone": "America/Bogota",
            "duration_minutes": 120,
            "capacity": 500,
        }
        created = self.events.post(
            "/v1/events", json=body, headers={**organizer, "Idempotency-Key": str(uuid.uuid4())}
        )
        self.assertEqual(created.status_code, 201, created.text)
        event_id = created.json()["event_id"]
        published = self.events.post(
            f"/v1/events/{event_id}/publish",
            headers={**organizer, "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(published.status_code, 200, published.text)
        return str(event_id)

    def inbox(self, athlete: dict[str, str]) -> list[dict[str, Any]]:
        response = self.bff.get(INBOX, headers=athlete)
        self.assertEqual(response.status_code, 200, response.text)
        items: list[dict[str, Any]] = response.json()["items"]
        return items

    def test_published_event_reaches_matching_athletes_once(self) -> None:
        """CA1, CA2 y CA3: deporte y ciudad, preferencias y entregas repetidas."""
        runner = self.sign_up("athlete")
        self.profile(runner, city="Bogotá", sport="running")
        elsewhere = self.sign_up("athlete")
        self.profile(elsewhere, city="Medellín", sport="running")
        cyclist = self.sign_up("athlete")
        self.profile(cyclist, city="Bogotá", sport="cycling")
        muted = self.sign_up("athlete")
        self.profile(muted, city="bogota", sport="running")
        changed = self.bff.put(
            PREFERENCES,
            json={
                "event_matches": False,
                "event_reminders": True,
                "push_enabled": False,
                "expected_version": 0,
            },
            headers=muted,
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["version"], 1)

        event_id = self.publish_event(
            self.sign_up("organizer"), discipline="Media Maratón", city="BOGOTÁ"
        )
        self.relay()  # evento → worker → notificaciones en outbox
        self.relay()  # notificaciones → bandeja

        items = self.inbox(runner)
        self.assertEqual(len(items), 1)
        self.assertEqual(
            {key: items[0][key] for key in ("type", "priority", "event_id", "read_at")},
            {
                "type": "EVENT_PUBLISHED_MATCH",
                "priority": "NORMAL",
                "event_id": event_id,
                "read_at": None,
            },
        )
        for other in (elsewhere, cyclist, muted):
            self.assertEqual(self.inbox(other), [])

        # Pub/Sub repite la entrega del evento publicado: ninguna notificación nueva.
        again = Delivery(self.published[0])
        self.handler(again)
        self.assertTrue(again.acked)
        self.relay()
        self.assertEqual(self.count(NotificationRow), 1)
        self.assertEqual(len(self.inbox(runner)), 1)

        read = self.bff.post(f"{INBOX}/{items[0]['notification_id']}/read", headers=runner)
        self.assertEqual(read.status_code, 200, read.text)
        self.assertIsNotNone(read.json()["read_at"])
        self.assertEqual(self.inbox(runner)[0]["read_at"], read.json()["read_at"])
        foreign = self.bff.post(f"{INBOX}/{items[0]['notification_id']}/read", headers=elsewhere)
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign.json()["code"], "notification_not_found")

    def test_registered_athlete_gets_the_24h_reminder_once(self) -> None:
        """CA1 (recordatorio) y CA3 (corrida repetida del planificador)."""
        athlete = self.sign_up("athlete")
        event_id = self.add_event()
        self.assertEqual(self.enroll(athlete, event_id).status_code, 201)
        starts_at = datetime.now(UTC) + timedelta(hours=24)
        with self.writes() as session, session.begin():
            session.execute(
                update(EventRow)
                .where(EventRow.event_id == uuid.UUID(event_id))
                .values(starts_at=starts_at, ends_at=starts_at + timedelta(minutes=180))
            )
        reminders = GenerateEventReminders(
            unit_of_work=lambda: SqlAlchemyNotificationUnitOfWork(self.writes), clock=SystemClock()
        )

        self.assertEqual(reminders().generated, 1)
        self.assertEqual(reminders().generated, 0)
        self.relay()

        items = self.inbox(athlete)
        self.assertEqual(
            [(item["type"], item["priority"], item["event_id"]) for item in items],
            [("EVENT_REMINDER_24H", "HIGH", event_id)],
        )

    def test_edge_rejects_other_actors_and_push_without_fcm(self) -> None:
        organizer = self.sign_up("organizer")
        self.assertEqual(self.bff.get(INBOX, headers=organizer).status_code, 403)
        athlete = self.sign_up("athlete")
        self.assertEqual(self.bff.get(PREFERENCES, headers=athlete).json()["version"], 0)
        push = self.bff.put(
            "/v1/me/push-tokens",
            json={"token": "dispositivo-sintetico", "platform": "android"},
            headers=athlete,
        )
        self.assertEqual(push.status_code, 409)
        self.assertEqual(push.json()["code"], "push_not_enabled")


if __name__ == "__main__":
    unittest.main()
