"""HU018 en Eventos (T-HU018-BE-EVT): el `service_provider` consulta `GET /v1/events` para la
fuente `EVENT_PUBLISHED` del calendario integrado (ADR-039), sin infraestructura. Misma
semántica que el deportista (ADR-031): solo `PUBLISHED`, superposición, filtros, 92 días,
keyset y `from ≤ to`; entra por `calendar.integrated.view` y nunca consulta Afiliación."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from test_create_event import VERIFIER, token
from test_published_events import NOV_1, NOV_30, Capabilities, InMemoryPublishedReader, event

from events_management.api.published_events import build_published_events_router
from events_management.application.errors import PermissionRequired
from events_management.application.ports import Caller
from events_management.application.queries.published_events import (
    EventsFilters,
    InvalidEventsQuery,
    ListPublishedEvents,
)
from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings


class Permissions:
    """Concede exactamente las parejas (actor, permiso) indicadas."""

    def __init__(self, *granted: tuple[str, str]) -> None:
        self.granted = set(granted)

    def allows(self, actor_type: str, permission: str) -> bool:
        return (actor_type, permission) in self.granted


PROVIDER_GRANT = ("service_provider", "calendar.integrated.view")


class ProviderCalendarTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = Caller(uuid4(), "service_provider")
        self.capabilities = Capabilities()
        self.permissions = Permissions(PROVIDER_GRANT)
        self.inside = event(datetime(2026, 11, 15, 12, tzinfo=UTC))
        self.crossing_start = event(datetime(2026, 11, 1, 3, tzinfo=UTC))  # termina 06:00
        self.touching_end = event(NOV_30)  # empieza justo en `to`
        self.draft = event(datetime(2026, 11, 20, 12, tzinfo=UTC), status="DRAFT")
        self.events = [
            self.inside,
            self.crossing_start,
            self.touching_end,
            event(datetime(2026, 10, 31, 0, tzinfo=UTC)),  # termina antes de `from`
            event(datetime(2026, 12, 2, 0, tzinfo=UTC)),  # empieza después de `to`
            self.draft,
        ]
        self.list = ListPublishedEvents(
            reader=InMemoryPublishedReader(self.events),
            capabilities=self.capabilities,
            permissions=self.permissions,
        )

    def ids(self, filters: EventsFilters) -> list[UUID]:
        return [item.event_id for item in self.list(self.provider, filters).items]

    def test_sees_only_overlapping_published_events_in_order(self) -> None:
        """Mismo resultado que el deportista; el borrador nunca aparece."""
        self.assertEqual(
            self.ids(EventsFilters(NOV_1, NOV_30)),
            [self.crossing_start.event_id, self.inside.event_id, self.touching_end.event_id],
        )
        self.assertNotIn(self.draft.event_id, self.ids(EventsFilters(NOV_1, NOV_30)))

    def test_same_result_as_athlete(self) -> None:
        athlete = Caller(uuid4(), "athlete")
        self.capabilities.granted.add(athlete.user_id)
        filters = EventsFilters(NOV_1, NOV_30, discipline="running", city="bogota")

        self.assertEqual(self.list(self.provider, filters), self.list(athlete, filters))

    def test_needs_integrated_calendar_permission_and_never_affiliation(self) -> None:
        self.ids(EventsFilters(NOV_1, NOV_30))
        self.assertEqual(self.capabilities.calls, [])
        for granted in (
            (),
            (("service_provider", "events.calendar.view"),),  # el permiso del organizador no basta
        ):
            self.permissions.granted = set(granted)
            with self.subTest(granted=granted), self.assertRaises(PermissionRequired):
                self.ids(EventsFilters(NOV_1, NOV_30))
        self.assertEqual(self.capabilities.calls, [])

    def test_organizer_keeps_its_own_permission(self) -> None:
        """`calendar.integrated.view` no basta al organizador: necesita `events.calendar.view`."""
        self.permissions.granted = {("organizer", "calendar.integrated.view")}
        with self.assertRaises(PermissionRequired):
            self.list(Caller(uuid4(), "organizer"), EventsFilters(NOV_1, NOV_30))

    def test_filters(self) -> None:
        cycling = replace(event(datetime(2026, 11, 16, 12, tzinfo=UTC)), discipline="Ciclismo")
        medellin = replace(event(datetime(2026, 11, 17, 12, tzinfo=UTC)), location_city="Medellín")
        self.events.extend((cycling, medellin))

        self.assertEqual(
            self.ids(EventsFilters(NOV_1, NOV_30, discipline=" CICLISMO ")), [cycling.event_id]
        )
        self.assertEqual(
            self.ids(EventsFilters(NOV_1, NOV_30, city="medellin")), [medellin.event_id]
        )
        self.assertEqual(self.ids(EventsFilters(NOV_1, NOV_30, city="Cali")), [])

    def test_period_and_limits_follow_adr_031(self) -> None:
        """`from == to` se admite (como el deportista); 92 días como máximo."""
        instant = datetime(2026, 11, 15, 13, tzinfo=UTC)
        self.assertEqual(self.ids(EventsFilters(instant, instant)), [self.inside.event_id])
        self.ids(EventsFilters(NOV_1, NOV_1 + timedelta(days=92)))
        naive = datetime(2026, 11, 1)
        cases = (
            (EventsFilters(NOV_30, NOV_1), "to", "ends_before_starts"),
            (EventsFilters(NOV_1, NOV_1 + timedelta(days=92, seconds=1)), "to", "out_of_range"),
            (EventsFilters(naive, NOV_30), "from", "invalid_datetime"),
            (EventsFilters(NOV_1, NOV_30, 201), "limit", "out_of_range"),
            (EventsFilters(NOV_1, NOV_30, 0), "limit", "out_of_range"),
            (EventsFilters(NOV_1, NOV_30, 10, "no-es-cursor"), "cursor", "invalid_cursor"),
            (EventsFilters(NOV_1, NOV_30, discipline="x"), "discipline", "invalid_length"),
            (EventsFilters(NOV_1, NOV_30, city="c" * 81), "city", "invalid_length"),
        )
        for filters, field, issue in cases:
            with self.subTest(field=field, issue=issue), self.assertRaises(InvalidEventsQuery) as e:
                self.ids(filters)
            self.assertEqual((e.exception.field, e.exception.issue), (field, issue))

    def test_keyset_pages_without_losses_or_repetitions(self) -> None:
        tied = datetime(2026, 11, 20, 12, tzinfo=UTC)
        self.events.extend(event(tied) for _ in range(5))
        seen: list[UUID] = []
        cursor = None
        while True:
            page = self.list(self.provider, EventsFilters(NOV_1, NOV_30, limit=2, cursor=cursor))
            seen += [item.event_id for item in page.items]
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        expected = sorted(
            (e for e in self.events if e.status == "PUBLISHED" and e.ends_at >= NOV_1),
            key=lambda e: (e.starts_at, e.event_id),
        )
        self.assertEqual(seen, [e.event_id for e in expected if e.starts_at <= NOV_30])
        self.assertNotIn(self.draft.event_id, seen)


class ProviderCalendarApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider_id = uuid4()
        self.permissions = Permissions(PROVIDER_GRANT)
        self.capabilities = Capabilities()
        self.published = event(datetime(2026, 11, 15, 12, tzinfo=UTC))
        self.draft = event(datetime(2026, 11, 16, 12, tzinfo=UTC), status="DRAFT")
        app = create_service_app(
            service_name="events-management",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_published_events_router(
                    verify_token=VERIFIER.verify,
                    list_events=ListPublishedEvents(
                        reader=InMemoryPublishedReader([self.published, self.draft]),
                        capabilities=self.capabilities,
                        permissions=self.permissions,
                    ),
                )
            ],
        )
        self.client = TestClient(app)

    def get(self, authorization: str | None = "token", **params: str) -> Any:
        query = {"from": "2026-11-01T00:00:00-05:00", "to": "2026-11-30T23:59:59-05:00", **params}
        headers = {}
        if authorization == "token":
            headers["Authorization"] = f"Bearer {token(self.provider_id, 'service_provider')}"
        elif authorization is not None:
            headers["Authorization"] = authorization
        return self.client.get("/v1/events", params=query, headers=headers)

    def test_contract_without_drafts(self) -> None:
        response = self.get()

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        [item] = body["items"]
        self.assertEqual(item["event_id"], str(self.published.event_id))
        self.assertEqual(item["status"], "PUBLISHED")
        self.assertEqual(
            set(item),
            {
                "event_id",
                "name",
                "discipline",
                "location",
                "starts_at",
                "ends_at",
                "time_zone",
                "status",
                "version",
                "fee",
            },
        )
        self.assertNotIn(str(self.draft.event_id), response.text)
        self.assertNotIn("organizer_id", response.text)
        self.assertIsNone(body["next_cursor"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.capabilities.calls, [])

    def test_same_instant_period_is_accepted(self) -> None:
        response = self.get(**{"from": "2026-11-15T13:00:00Z", "to": "2026-11-15T13:00:00Z"})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["items"]), 1)

    def test_errors(self) -> None:
        inverted = self.get(**{"from": "2026-11-30T00:00:00Z", "to": "2026-11-01T00:00:00Z"})
        long = self.get(**{"from": "2026-11-01T00:00:00Z", "to": "2027-02-02T00:00:00Z"})
        blank = self.get(discipline=" ")
        bad_limit = self.get(limit="201")
        anonymous = self.get(authorization=None)
        self.permissions.granted.clear()
        without_permission = self.get()

        self.assertEqual(
            inverted.json()["details"], [{"field": "query.to", "issue": "ends_before_starts"}]
        )
        self.assertEqual(long.json()["details"], [{"field": "query.to", "issue": "out_of_range"}])
        self.assertEqual(
            blank.json()["details"], [{"field": "query.discipline", "issue": "invalid_length"}]
        )
        self.assertEqual(bad_limit.status_code, 422)
        self.assertEqual(
            (anonymous.status_code, anonymous.json()["code"]), (401, "invalid_access_token")
        )
        self.assertEqual(
            (without_permission.status_code, without_permission.json()["code"]),
            (403, "permission_required"),
        )
        self.assertEqual(self.capabilities.calls, [])


if __name__ == "__main__":
    unittest.main()
