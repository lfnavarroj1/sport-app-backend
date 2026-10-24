"""Componente HU022 en web: BFF → Eventos (inscritos de un evento propio) + Usuarios
(`POST /v1/users/directory-lookup`) con servicios reales y PostgreSQL migrado (ADR-039).

Mismo montaje que HU024 (`EventFlowCase`). Los deportistas se inscriben con el servicio real de
Eventos (HU041; su canal es móvil) y las proyecciones de Eventos y de Usuarios se aplican con
sus casos de uso, sin Pub/Sub (el transporte lo cubren las pruebas de HU024 y de Usuarios).
"""

import json
import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_event_publication_flow import ORIGIN, EventFlowCase

from bff_web.main import create_app as create_bff
from bff_web.settings import BffWebSettings
from events_management.application.messages import parse_sport_event_message
from events_management.application.projections.event_projection import EventsProjector
from events_management.infrastructure.clock import SystemClock
from events_management.infrastructure.persistence_read.events import (
    SqlAlchemyProjectionUnitOfWork,
)
from events_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from events_management.workers.outbox_publisher import build_envelope

DIRECTORY = "/v1/users/directory-lookup"


class UsersWithoutDirectory(httpx.BaseTransport):
    """Usuarios real salvo el directorio de nombres, que no responde (CA6)."""

    def __init__(self, users: TestClient) -> None:
        self.users = users
        self.directory_calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == DIRECTORY:
            self.directory_calls += 1
            raise httpx.ConnectError("usuarios caído")
        answer = self.users.request(
            request.method,
            request.url.path,
            headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
            content=request.content,
        )
        return httpx.Response(answer.status_code, headers=answer.headers, content=answer.content)


class EventAttendeesFlowTest(EventFlowCase):
    def project_events(self) -> None:
        store = SqlAlchemyOutboxStore(self.writes)
        project = EventsProjector(
            unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(self.reads),
            clock=SystemClock(),
        )
        for event in store.claim_batch("w-test", 100, 30):
            project(parse_sport_event_message(json.loads(json.dumps(build_envelope(event)))))
            store.mark_published(event.event_id, "w-test")

    def published_event(self, organizer: dict[str, str]) -> str:
        event_id = self.draft(organizer)
        published = self.publish(organizer, event_id, str(uuid.uuid4()))
        self.assertEqual(published.status_code, 200, published.text)
        return event_id

    def register(self, athlete: dict[str, str], event_id: str) -> None:
        response = self.events.post(
            f"/v1/events/{event_id}/registrations",
            headers={**athlete, "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(response.status_code, 201, response.text)

    def attendees(
        self, client: TestClient, user: dict[str, str], event_id: str, **params: str
    ) -> Any:
        return client.get(f"/v1/me/events/{event_id}/registrations", params=params, headers=user)

    def test_organizer_sees_attendees_with_names_and_counts(self) -> None:
        """CA1, CA2, CA3 y CA5: nombres, estado, fecha, `total` y `by_status`; ajeno → `404`."""
        organizer = self.session_for("organizer")
        event_id = self.published_event(organizer)
        empty_event = self.published_event(organizer)
        ana = self.session_for("athlete", "Ana Inscrita")
        ana_id = self.last_user_id
        luis = self.session_for("athlete", "Luis Inscrito")
        luis_id = self.last_user_id
        for athlete in (ana, luis):
            self.register(athlete, event_id)
        self.project_events()
        self.project_users()

        response = self.attendees(self.bff, organizer, event_id)
        empty = self.attendees(self.bff, organizer, empty_event, status="ALL")
        foreign = self.attendees(self.bff, self.session_for("organizer"), event_id)
        athlete = self.attendees(self.bff, ana, event_id)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body = response.json()
        self.assertEqual(
            [(item["athlete_id"], item["display_name"]) for item in body["items"]],
            [(ana_id, "Ana Inscrita"), (luis_id, "Luis Inscrito")],
        )
        self.assertEqual({item["status"] for item in body["items"]}, {"REGISTERED"})
        self.assertTrue(all(item["registered_at"].endswith("Z") for item in body["items"]))
        self.assertEqual((body["total"], body["by_status"]), (2, {"REGISTERED": 2}))
        self.assertEqual(body["sources"], [{"source": "users", "status": "OK"}])
        self.assertNotIn("@example.com", response.text)
        self.assertEqual(
            (empty.json()["items"], empty.json()["total"], empty.json()["sources"]),
            ([], 0, [{"source": "users", "status": "OK"}]),
        )
        self.assertEqual((foreign.status_code, foreign.json()["code"]), (404, "event_not_found"))
        self.assertEqual(
            (athlete.status_code, athlete.json()["code"]), (403, "permission_required")
        )

    def test_without_users_the_list_has_null_names(self) -> None:
        """CA6: Usuarios no responde → `display_name: null` y `sources[users] = UNAVAILABLE`."""
        organizer = self.session_for("organizer")
        event_id = self.published_event(organizer)
        self.register(self.session_for("athlete", "Marta Inscrita"), event_id)
        self.project_events()
        self.project_users()
        transport = UsersWithoutDirectory(self.users)
        degraded = self.enterContext(
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
                    users_http=httpx.Client(
                        transport=transport, base_url="http://users-management"
                    ),
                    events_http=self.events,
                ),
                base_url="https://bff-web",
                headers={"Origin": ORIGIN},
            )
        )

        response = self.attendees(degraded, organizer, event_id)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual([item["display_name"] for item in body["items"]], [None])
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["sources"], [{"source": "users", "status": "UNAVAILABLE"}])
        self.assertEqual(transport.directory_calls, 1)


if __name__ == "__main__":
    unittest.main()
