"""HU024 por HTTP con dobles en memoria: publicar, eventos propios y visibilidad del detalle."""

import unittest
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from fakes import NOW, FixedClock, InMemoryStore
from fastapi.testclient import TestClient
from publication_fakes import (
    InMemoryOwnEventReader,
    InMemoryPublicationUnitOfWork,
    PublishPermissions,
    draft,
)
from registration_fakes import InMemoryEventDetailReader, calendar_view
from test_create_event import VERIFIER, token

from events_management.api.publication import build_publication_router
from events_management.api.registrations import build_registrations_router
from events_management.application.commands.publish_event import PublishEvent
from events_management.application.commands.register_for_event import RegisterForEvent
from events_management.application.ports import EventDetailView
from events_management.application.queries.my_events import ListMyEvents
from events_management.application.queries.registrations import (
    GetPublishedEvent,
    ListMyRegistrations,
)
from events_management.domain.sport_events.model import EventStatus, SportEvent
from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings

AGGREGATE_FIELDS = {
    "event_id",
    "organizer_id",
    "name",
    "description",
    "discipline",
    "location",
    "starts_at",
    "ends_at",
    "time_zone",
    "duration_minutes",
    "capacity",
    "status",
    "version",
    "created_at",
    "updated_at",
    "published_at",
}


class PublicationApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryStore()
        self.clock = FixedClock()
        self.organizer_id = uuid4()
        self.event = draft(self.organizer_id)
        self.store.events[self.event.event_id] = self.event
        self.own: list[SportEvent] = []
        self.details: dict[UUID, EventDetailView] = {}
        self.owners: dict[UUID, UUID] = {}
        app = create_service_app(
            service_name="events-management",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_publication_router(
                    verify_token=VERIFIER.verify,
                    publish_event=PublishEvent(
                        unit_of_work=lambda: InMemoryPublicationUnitOfWork(self.store),
                        permissions=PublishPermissions(),
                        clock=self.clock,
                    ),
                    list_my_events=ListMyEvents(reader=InMemoryOwnEventReader(self.own)),
                ),
                build_registrations_router(
                    verify_token=VERIFIER.verify,
                    get_event=GetPublishedEvent(
                        reader=InMemoryEventDetailReader(self.details, self.owners)
                    ),
                    register=RegisterForEvent(unit_of_work=lambda: None, clock=self.clock),  # type: ignore[arg-type,return-value]
                    list_registrations=ListMyRegistrations(reader=None),  # type: ignore[arg-type]
                ),
            ],
        )
        self.client = TestClient(app)

    def headers(
        self, actor: str | None = "organizer", user: UUID | None = None, **extra: str
    ) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(user or self.organizer_id, actor)}", **extra}

    def publish(self, event_id: UUID | None = None, key: str | None = None, **headers: Any) -> Any:
        return self.client.post(
            f"/v1/events/{event_id or self.event.event_id}/publish",
            headers=self.headers(**headers, **({"Idempotency-Key": key} if key else {})),
        )

    # --- POST /v1/events/{event_id}/publish ---

    def test_publish_returns_the_full_aggregate(self) -> None:
        """CA1: `200` con el agregado completo, `PUBLISHED`, versión 2 y `published_at`."""
        key = str(uuid4())

        response = self.publish(key=key)
        replay = self.publish(key=key)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(set(body), AGGREGATE_FIELDS)
        self.assertEqual((body["status"], body["version"]), ("PUBLISHED", 2))
        self.assertEqual(body["published_at"], "2026-10-12T21:00:00Z")
        self.assertEqual(body["starts_at"], "2026-11-15T12:00:00Z")
        self.assertNotIn("Idempotent-Replayed", response.headers)
        self.assertEqual((replay.status_code, replay.json()), (200, body))
        self.assertEqual(replay.headers["Idempotent-Replayed"], "true")

    def test_publish_errors(self) -> None:
        """CA2, CA3, CA4 y CA6 por HTTP."""
        late = draft(self.organizer_id, starts_in=timedelta(minutes=30))
        foreign = draft(uuid4())
        for item in (late, foreign):
            self.store.events[item.event_id] = item
        reused = str(uuid4())
        self.publish(key=reused)
        self.clock.advance(timedelta(hours=1))

        cases: dict[str, tuple[Any, int, str]] = {
            "anónimo": (
                self.client.post(f"/v1/events/{late.event_id}/publish"),
                401,
                "invalid_access_token",
            ),
            "deportista": (
                self.publish(late.event_id, str(uuid4()), actor="athlete"),
                403,
                "permission_required",
            ),
            "sin clave": (self.publish(late.event_id), 422, "validation_error"),
            "ajeno": (self.publish(foreign.event_id, str(uuid4())), 404, "event_not_found"),
            "inexistente": (self.publish(uuid4(), str(uuid4())), 404, "event_not_found"),
            "clave con otro evento": (
                self.publish(late.event_id, reused),
                409,
                "idempotency_key_reused",
            ),
            "inicio pasado": (self.publish(late.event_id, str(uuid4())), 422, "validation_error"),
        }
        for name, (response, status, code) in cases.items():
            with self.subTest(case=name):
                self.assertEqual((response.status_code, response.json()["code"]), (status, code))
        self.assertEqual(
            cases["sin clave"][0].json()["details"],
            [{"field": "Idempotency-Key", "issue": "required"}],
        )
        self.assertEqual(
            cases["inicio pasado"][0].json()["details"],
            [{"field": "starts_at", "issue": "not_in_future"}],
        )
        invalid_key = self.publish(late.event_id, "no-es-uuid")
        self.assertEqual(
            invalid_key.json()["details"], [{"field": "Idempotency-Key", "issue": "unknown_value"}]
        )
        self.assertEqual(self.store.events[late.event_id].status, EventStatus.DRAFT)
        self.assertEqual(self.store.events[foreign.event_id].status, EventStatus.DRAFT)

    # --- GET /v1/me/events ---

    def test_my_events_filters_and_pages_only_own_events(self) -> None:
        """CA6: solo los propios, filtro `status`, orden `starts_at`, `event_id` y keyset."""
        published = [
            draft(self.organizer_id, starts_in=timedelta(days=day)).publish(NOW) for day in (3, 1)
        ]
        drafts = [draft(self.organizer_id, starts_in=timedelta(days=day)) for day in (2, 4)]
        self.own.extend([*published, *drafts, draft(uuid4(), starts_in=timedelta(days=1))])

        first = self.client.get("/v1/me/events", params={"limit": 3}, headers=self.headers())
        rest = self.client.get(
            "/v1/me/events",
            params={"limit": 3, "cursor": first.json()["next_cursor"]},
            headers=self.headers(),
        )
        only_drafts = self.client.get(
            "/v1/me/events", params={"status": "DRAFT"}, headers=self.headers()
        )

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.headers["Cache-Control"], "no-store")
        ids = [i["event_id"] for i in first.json()["items"] + rest.json()["items"]]
        expected = sorted([*published, *drafts], key=lambda e: e.details.starts_at)
        self.assertEqual(ids, [str(e.event_id) for e in expected])
        self.assertIsNone(rest.json()["next_cursor"])
        self.assertEqual(set(first.json()["items"][0]), AGGREGATE_FIELDS)
        self.assertEqual({i["status"] for i in only_drafts.json()["items"]}, {"DRAFT"})
        self.assertEqual({i["published_at"] for i in only_drafts.json()["items"]}, {None})
        self.assertTrue(
            all(i["published_at"] for i in first.json()["items"] if i["status"] == "PUBLISHED")
        )

    def test_my_events_errors(self) -> None:
        athlete = self.client.get("/v1/me/events", headers=self.headers("athlete"))
        bad = {
            "status": self.client.get(
                "/v1/me/events", params={"status": "CANCELLED"}, headers=self.headers()
            ),
            "limit": self.client.get(
                "/v1/me/events", params={"limit": 101}, headers=self.headers()
            ),
            "cursor": self.client.get(
                "/v1/me/events", params={"cursor": "x"}, headers=self.headers()
            ),
        }

        self.assertEqual(
            (athlete.status_code, athlete.json()["code"]), (403, "permission_required")
        )
        for name, response in bad.items():
            with self.subTest(param=name):
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "validation_error")
        self.assertEqual(
            bad["cursor"].json()["details"], [{"field": "query.cursor", "issue": "invalid_cursor"}]
        )

    # --- GET /v1/events/{event_id} (ADR-033) ---

    def test_draft_detail_only_for_its_organizer(self) -> None:
        """CA3: el `DRAFT` solo lo ve su propietario; un `PUBLISHED`, cualquier actor."""
        draft_id, published_id = uuid4(), uuid4()
        self.details[draft_id] = EventDetailView(
            calendar_view(draft_id, NOW + timedelta(days=10), status="DRAFT"), "Borrador", 10, None
        )
        self.details[published_id] = EventDetailView(
            calendar_view(published_id, NOW + timedelta(days=10)), "Publicado", 10, None
        )
        self.owners[draft_id] = self.organizer_id

        own = self.client.get(f"/v1/events/{draft_id}", headers=self.headers())
        others = [
            self.client.get(f"/v1/events/{draft_id}", headers=self.headers(actor, uuid4()))
            for actor in ("organizer", "athlete", "service_provider")
        ]
        public = self.client.get(
            f"/v1/events/{published_id}", headers=self.headers("service_provider", uuid4())
        )

        self.assertEqual((own.status_code, own.json()["status"]), (200, "DRAFT"))
        for response in others:
            self.assertEqual(
                (response.status_code, response.json()["code"]), (404, "event_not_found")
            )
        self.assertEqual((public.status_code, public.json()["status"]), (200, "PUBLISHED"))


if __name__ == "__main__":
    unittest.main()
