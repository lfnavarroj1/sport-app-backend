"""HU023 en Eventos: calendario del organizador sobre `GET /v1/events` (ADR-040), sin
infraestructura. Las reglas de HU035 para el deportista están en test_published_events."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from test_create_event import VERIFIER, token
from test_published_events import (
    NOV_1,
    NOV_30,
    Capabilities,
    InMemoryPublishedReader,
    OrganizerCalendar,
    event,
)

from events_management.api.published_events import build_published_events_router
from events_management.application.errors import PermissionRequired
from events_management.application.ports import CalendarEventView, Caller
from events_management.application.queries.published_events import (
    EventsFilters,
    InvalidEventsQuery,
    ListPublishedEvents,
)
from events_management.domain.sport_events.terms import ACCENTED, PLAIN, normalize_term
from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings


def located(when: datetime, discipline: str, city: str) -> CalendarEventView:
    return replace(event(when), discipline=discipline, location_city=city)


class NormalizeTermTest(unittest.TestCase):
    def test_ignores_case_accents_and_outer_spaces(self) -> None:
        cases = {
            "  Bogotá ": "bogota",
            "MEDELLÍN": "medellin",
            "Ciclismo de Montaña": "ciclismo de montana",
            "São Paulo": "sao paulo",
            "Ñandú Çà": "nandu ca",
            "Running": "running",
        }
        for raw, normalized in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_term(raw), normalized)

    def test_equivalence_table_is_one_to_one_and_ascii(self) -> None:
        """La consulta SQL usa `translate(col, ACCENTED, PLAIN)`: misma longitud y destino ASCII."""
        self.assertEqual(len(ACCENTED), len(PLAIN))
        self.assertTrue(PLAIN.isascii())
        self.assertIn("ñ", ACCENTED)
        self.assertNotIn("ø", ACCENTED)  # sin descomposición canónica: no se toca


class OrganizerCalendarTest(unittest.TestCase):
    def setUp(self) -> None:
        self.organizer = Caller(uuid4(), "organizer")
        self.capabilities = Capabilities()
        self.permissions = OrganizerCalendar()
        day = datetime(2026, 11, 15, 12, tzinfo=UTC)
        self.bogota_running = located(day, "Running", "Bogotá")
        self.bogota_cycling = located(day + timedelta(days=1), "Ciclismo", "BOGOTA")
        self.medellin_running = located(day + timedelta(days=2), " running ", "Medellín")
        self.draft = replace(located(day, "Running", "Bogotá"), status="DRAFT")
        self.events = [
            self.bogota_running,
            self.bogota_cycling,
            self.medellin_running,
            self.draft,
        ]
        self.list = ListPublishedEvents(
            reader=InMemoryPublishedReader(self.events),
            capabilities=self.capabilities,
            permissions=self.permissions,
        )

    def ids(self, **filters: Any) -> list[UUID]:
        page = self.list(self.organizer, EventsFilters(NOV_1, NOV_30, **filters))
        return [item.event_id for item in page.items]

    def test_filters_by_normalized_discipline_and_city(self) -> None:
        """CA1: solo publicados que cumplen los filtros, sin borradores."""
        self.assertEqual(
            self.ids(city="bogotá"), [self.bogota_running.event_id, self.bogota_cycling.event_id]
        )
        self.assertEqual(
            self.ids(discipline="RUNNING"),
            [self.bogota_running.event_id, self.medellin_running.event_id],
        )
        self.assertEqual(
            self.ids(discipline="running", city="  Bogota "), [self.bogota_running.event_id]
        )
        self.assertEqual(self.ids(), [e.event_id for e in self.events[:3]])
        self.assertEqual(self.ids(city="Cali"), [])

    def test_organizer_needs_calendar_permission_and_never_affiliation(self) -> None:
        self.ids()
        self.assertEqual(self.capabilities.calls, [])
        self.permissions.granted = False
        with self.assertRaises(PermissionRequired):
            self.ids()

    def test_period_rules(self) -> None:
        """CA3: `from ≥ to` y más de 92 días son inválidos para el organizador; el deportista
        conserva `from == to` (ADR-031)."""
        exactly_92 = EventsFilters(NOV_1, NOV_1 + timedelta(days=92))
        self.list(self.organizer, exactly_92)
        cases = (
            (EventsFilters(NOV_1, NOV_1), "to", "ends_before_starts"),
            (EventsFilters(NOV_30, NOV_1), "to", "ends_before_starts"),
            (EventsFilters(NOV_1, NOV_1 + timedelta(days=92, seconds=1)), "to", "out_of_range"),
            (EventsFilters(NOV_1, NOV_30, discipline="  x "), "discipline", "invalid_length"),
            (EventsFilters(NOV_1, NOV_30, city="c" * 81), "city", "invalid_length"),
        )
        for filters, field, issue in cases:
            with self.subTest(field=field, issue=issue), self.assertRaises(InvalidEventsQuery) as e:
                self.list(self.organizer, filters)
            self.assertEqual((e.exception.field, e.exception.issue), (field, issue))
        athlete = Caller(uuid4(), "athlete")
        self.capabilities.granted.add(athlete.user_id)
        self.list(athlete, EventsFilters(NOV_1, NOV_1))
        with self.assertRaises(InvalidEventsQuery):
            self.list(athlete, EventsFilters(NOV_1, NOV_1 + timedelta(days=93)))

    def test_keyset_pages_without_losses_or_repetitions(self) -> None:
        """CA4: orden estable `starts_at`, `event_id` aunque haya empates de inicio."""
        tied = datetime(2026, 11, 20, 12, tzinfo=UTC)
        self.events.extend(located(tied, "Running", "Cali") for _ in range(5))
        seen: list[UUID] = []
        cursor = None
        while True:
            page = self.list(self.organizer, EventsFilters(NOV_1, NOV_30, limit=2, cursor=cursor))
            seen += [item.event_id for item in page.items]
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        expected = sorted(
            (e for e in self.events if e.status == "PUBLISHED"),
            key=lambda e: (e.starts_at, e.event_id),
        )
        self.assertEqual(seen, [e.event_id for e in expected])


class OrganizerCalendarApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.organizer_id = uuid4()
        self.events = [located(datetime(2026, 11, 15, 12, tzinfo=UTC), "Running", "Bogotá")]
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
                        capabilities=Capabilities(),
                        permissions=OrganizerCalendar(),
                    ),
                )
            ],
        )
        self.client = TestClient(app)

    def get(self, actor: str = "organizer", **params: str) -> Any:
        query = {"from": "2026-11-01T00:00:00Z", "to": "2026-11-30T00:00:00Z", **params}
        return self.client.get(
            "/v1/events",
            params=query,
            headers={"Authorization": f"Bearer {token(self.organizer_id, actor)}"},
        )

    def test_filters_and_shape(self) -> None:
        found = self.get(discipline="running", city="bogota")
        empty = self.get(city="Cali")

        self.assertEqual(found.status_code, 200, found.text)
        [item] = found.json()["items"]
        self.assertEqual(item["location"]["city"], "Bogotá")
        self.assertNotIn("organizer_id", found.text)
        self.assertEqual(found.headers["Cache-Control"], "no-store")
        self.assertEqual(empty.json()["items"], [])

    def test_errors(self) -> None:
        same = self.get(**{"to": "2026-11-01T00:00:00Z"})
        long = self.get(**{"to": "2027-02-02T00:00:00Z"})
        blank = self.get(city=" ")
        provider = self.get("service_provider")

        self.assertEqual(
            same.json()["details"], [{"field": "query.to", "issue": "ends_before_starts"}]
        )
        self.assertEqual(long.json()["details"], [{"field": "query.to", "issue": "out_of_range"}])
        self.assertEqual(
            blank.json()["details"], [{"field": "query.city", "issue": "invalid_length"}]
        )
        self.assertEqual(
            (provider.status_code, provider.json()["code"]), (403, "permission_required")
        )


if __name__ == "__main__":
    unittest.main()
