"""HU035 en Eventos: eventos publicados por periodo (ADR-031), sin infraestructura."""

import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from test_create_event import VERIFIER, token

from events_management.api.published_events import build_published_events_router
from events_management.application.errors import (
    CapabilitiesUnavailable,
    CapabilityRequired,
    PermissionRequired,
)
from events_management.application.ports import (
    CalendarEventView,
    Caller,
    PublishedEventsQuery,
)
from events_management.application.queries.published_events import (
    EventsFilters,
    InvalidEventsQuery,
    ListPublishedEvents,
)
from events_management.domain.sport_events.terms import normalize_term
from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings

NOV_1 = datetime(2026, 11, 1, 5, 0, tzinfo=UTC)
NOV_30 = datetime(2026, 12, 1, 4, 59, 59, tzinfo=UTC)


class Capabilities:
    def __init__(self, granted: set[UUID] | None = None) -> None:
        self.granted = granted or set()
        self.unavailable = False
        self.calls: list[tuple[UUID, str]] = []

    def has_capability(self, user_id: UUID, capability: str) -> bool:
        self.calls.append((user_id, capability))
        if self.unavailable:
            raise CapabilitiesUnavailable()
        return user_id in self.granted


class OrganizerCalendar:
    """`actor-permissions.v1`: el organizador tiene `events.calendar.view` (HU023)."""

    def __init__(self, granted: bool = True) -> None:
        self.granted = granted

    def allows(self, actor_type: str, permission: str) -> bool:
        return self.granted and actor_type == "organizer" and permission == "events.calendar.view"


class InMemoryPublishedReader:
    """Mismas reglas que la consulta SQL: superposición, filtros normalizados, orden y keyset."""

    def __init__(self, events: list[CalendarEventView]) -> None:
        self.events = events

    def published(self, query: PublishedEventsQuery) -> list[CalendarEventView]:
        selected = sorted(
            (
                event
                for event in self.events
                if event.status == "PUBLISHED"
                and event.starts_at <= query.starts_before_or_at
                and event.ends_at >= query.ends_at_or_after
                and (
                    query.discipline is None or normalize_term(event.discipline) == query.discipline
                )
                and (query.city is None or normalize_term(event.location_city) == query.city)
                and (
                    query.after is None
                    or (event.starts_at, event.event_id)
                    > (query.after.starts_at, query.after.event_id)
                )
            ),
            key=lambda event: (event.starts_at, event.event_id),
        )
        return selected[: query.limit]


def event(starts_at: datetime, hours: int = 3, status: str = "PUBLISHED") -> CalendarEventView:
    return CalendarEventView(
        event_id=uuid4(),
        name="Carrera 10K Bogotá",
        discipline="Running",
        location_name="Parque Metropolitano Simón Bolívar",
        location_city="Bogotá",
        location_country_code="CO",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=hours),
        time_zone="America/Bogota",
        status=status,
        version=2,
    )


class ListPublishedEventsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.athlete = Caller(uuid4(), "athlete")
        self.capabilities = Capabilities({self.athlete.user_id})
        self.inside = event(datetime(2026, 11, 15, 12, tzinfo=UTC))
        self.crossing_start = event(datetime(2026, 11, 1, 3, tzinfo=UTC))  # termina 06:00
        self.touching_end = event(NOV_30)  # empieza justo en `to`
        self.events = [
            self.inside,
            self.crossing_start,
            self.touching_end,
            event(datetime(2026, 10, 31, 0, tzinfo=UTC)),  # termina antes de `from`
            event(datetime(2026, 12, 2, 0, tzinfo=UTC)),  # empieza después de `to`
            event(datetime(2026, 11, 20, 12, tzinfo=UTC), status="DRAFT"),
        ]
        self.list = ListPublishedEvents(
            reader=InMemoryPublishedReader(self.events),
            capabilities=self.capabilities,
            permissions=OrganizerCalendar(),
        )

    def test_overlapping_published_events_in_order(self) -> None:
        """CA1 y CA5: superposición en los bordes, sin borradores, orden estable."""
        page = self.list(self.athlete, EventsFilters(NOV_1, NOV_30))

        self.assertEqual(
            [item.event_id for item in page.items],
            [self.crossing_start.event_id, self.inside.event_id, self.touching_end.event_id],
        )
        self.assertIsNone(page.next_cursor)
        self.assertEqual(self.capabilities.calls, [(self.athlete.user_id, "calendar.sport.view")])

    def test_cursor_pages(self) -> None:
        first = self.list(self.athlete, EventsFilters(NOV_1, NOV_30, limit=2))
        second = self.list(self.athlete, EventsFilters(NOV_1, NOV_30, 2, first.next_cursor))

        self.assertEqual(len(first.items), 2)
        self.assertEqual([item.event_id for item in second.items], [self.touching_end.event_id])
        self.assertIsNone(second.next_cursor)

    def test_authorization(self) -> None:
        """CA5: deportista con `calendar.sport.view`; Afiliación caída no concede nada. Desde
        HU023 el organizador también entra (con `events.calendar.view`, ver test_event_calendar);
        cualquier otro actor sigue rechazado."""
        with self.assertRaises(PermissionRequired):
            self.list(Caller(uuid4(), "service_provider"), EventsFilters(NOV_1, NOV_30))
        with self.assertRaises(CapabilityRequired):
            self.list(Caller(uuid4(), "athlete"), EventsFilters(NOV_1, NOV_30))
        self.capabilities.unavailable = True
        with self.assertRaises(CapabilitiesUnavailable):
            self.list(self.athlete, EventsFilters(NOV_1, NOV_30))

    def test_invalid_queries(self) -> None:
        naive = datetime(2026, 11, 1)
        for filters, field, issue in (
            (EventsFilters(NOV_30, NOV_1), "to", "ends_before_starts"),
            (EventsFilters(naive, NOV_30), "from", "invalid_datetime"),
            (EventsFilters(NOV_1, NOV_30, 201), "limit", "out_of_range"),
            (EventsFilters(NOV_1, NOV_30, 10, "no-es-cursor"), "cursor", "invalid_cursor"),
        ):
            with self.subTest(issue=issue), self.assertRaises(InvalidEventsQuery) as raised:
                self.list(self.athlete, filters)
            self.assertEqual((raised.exception.field, raised.exception.issue), (field, issue))


class PublishedEventsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.athlete_id = uuid4()
        self.capabilities = Capabilities({self.athlete_id})
        self.events = [event(datetime(2026, 11, 15, 12, tzinfo=UTC))]
        app = create_service_app(
            service_name="events-management",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_published_events_router(
                    verify_token=VERIFIER.verify,
                    list_events=ListPublishedEvents(
                        reader=InMemoryPublishedReader(self.events),
                        capabilities=self.capabilities,
                        permissions=OrganizerCalendar(),
                    ),
                )
            ],
        )
        self.client = TestClient(app)

    def get(self, query: str, actor: str = "athlete", user: UUID | None = None) -> Any:
        return self.client.get(
            f"/v1/events?{query}",
            headers={"Authorization": f"Bearer {token(user or self.athlete_id, actor)}"},
        )

    def test_contract(self) -> None:
        response = self.get("from=2026-11-01T00:00:00-05:00&to=2026-11-30T23:59:59-05:00")

        self.assertEqual(response.status_code, 200, response.text)
        [item] = response.json()["items"]
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
            },
        )
        self.assertNotIn("organizer_id", response.text)
        self.assertEqual(
            (item["starts_at"], item["time_zone"]), ("2026-11-15T12:00:00Z", "America/Bogota")
        )
        self.assertEqual(
            item["location"],
            {"name": "Parque Metropolitano Simón Bolívar", "city": "Bogotá", "country_code": "CO"},
        )

    def test_errors(self) -> None:
        period = "from=2026-11-01T00:00:00-05:00&to=2026-11-30T23:59:59-05:00"
        provider = self.get(period, "service_provider")
        no_capability = self.get(period, user=uuid4())
        inverted = self.get("from=2026-11-30T00:00:00Z&to=2026-11-01T00:00:00Z")
        naive = self.get("from=2026-11-01T00:00:00&to=2026-11-30T00:00:00Z")
        self.capabilities.unavailable = True
        down = self.get(period)

        self.assertEqual(
            (provider.status_code, provider.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(
            (no_capability.status_code, no_capability.json()["code"]), (403, "capability_required")
        )
        self.assertEqual(
            inverted.json()["details"], [{"field": "query.to", "issue": "ends_before_starts"}]
        )
        self.assertEqual(
            naive.json()["details"], [{"field": "query.from", "issue": "invalid_datetime"}]
        )
        self.assertEqual((down.status_code, down.json()["code"]), (503, "upstream_unavailable"))


if __name__ == "__main__":
    unittest.main()
