"""HU023 con PostgreSQL real: filtros normalizados en SQL y keyset con 150 eventos (ADR-040)."""

import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from support import OrganizerOnly, event_databases

from events_management.application.ports import Caller
from events_management.application.queries.published_events import (
    EventsFilters,
    ListPublishedEvents,
)
from events_management.domain.sport_events.terms import normalize_term
from events_management.infrastructure.persistence_read.events import (
    SqlAlchemyPublishedEventReader,
)
from events_management.infrastructure.persistence_read.models import EventReadRow

START = datetime(2026, 11, 1, 5, 0, tzinfo=UTC)


class NoAffiliation:
    """El organizador nunca consulta Afiliación."""

    def has_capability(self, user_id: UUID, capability: str) -> bool:
        raise AssertionError("el organizador no consulta capacidades")


class EventCalendarPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = self.enterContext(event_databases("hu023_calendar"))
        self.organizer = Caller(uuid4(), "organizer")
        self.list = ListPublishedEvents(
            reader=SqlAlchemyPublishedEventReader(self.db.reads),
            capabilities=NoAffiliation(),
            permissions=OrganizerOnly(),
        )

    def add(
        self, starts_at: datetime, discipline: str, city: str, status: str = "PUBLISHED"
    ) -> UUID:
        event_id = uuid4()
        with self.db.reads() as session, session.begin():
            session.add(
                EventReadRow(
                    event_id=event_id,
                    organizer_id=uuid4(),
                    name="Evento sintético",
                    description="Descripción sintética de un evento masivo publicado.",
                    discipline=discipline,
                    location_name="Parque central",
                    location_address="Calle 1 # 2-3",
                    location_city=city,
                    location_country_code="CO",
                    starts_at=starts_at,
                    ends_at=starts_at + timedelta(hours=2),
                    time_zone="America/Bogota",
                    duration_minutes=120,
                    capacity=100,
                    status=status,
                    version=2 if status == "PUBLISHED" else 1,
                    created_at=START,
                    updated_at=START,
                    published_at=START if status == "PUBLISHED" else None,
                    projected_at=START,
                )
            )
        return event_id

    def pages(self, limit: int, **filters: str) -> list[UUID]:
        seen: list[UUID] = []
        cursor = None
        while True:
            page = self.list(
                self.organizer,
                EventsFilters(
                    START, START + timedelta(days=60), limit=limit, cursor=cursor, **filters
                ),
            )
            seen += [item.event_id for item in page.items]
            if page.next_cursor is None:
                return seen
            cursor = page.next_cursor

    def test_sql_filter_normalizes_like_the_domain(self) -> None:
        stored = ["Bogotá", "BOGOTA", "bogota", "Medellín", "São Paulo", "Peñalosa"]
        ids = {
            city: self.add(START + timedelta(hours=i), "Running", city)
            for i, city in enumerate(stored)
        }

        for query in ("bogotá", " Bogota ", "BOGOTÁ"):
            with self.subTest(query=query):
                found = set(self.pages(50, city=query))
                self.assertEqual(found, {ids["Bogotá"], ids["BOGOTA"], ids["bogota"]})
        for city in ("Medellín", "São Paulo", "Peñalosa"):
            with self.subTest(city=city):
                self.assertEqual(self.pages(50, city=normalize_term(city)), [ids[city]])

    def test_keyset_with_150_events_and_combined_filters(self) -> None:
        """CA1 y CA4: 150 publicados (con empates de inicio y borradores) paginados sin
        pérdidas ni repeticiones; filtros combinados."""
        expected_all: list[tuple[datetime, UUID]] = []
        expected_combined: list[tuple[datetime, UUID]] = []
        for index in range(150):
            starts_at = START + timedelta(hours=index // 3)  # tres eventos por instante
            discipline = "Ciclismo" if index % 2 else "Running"
            city = "Medellín" if index % 3 else "Bogotá"
            event_id = self.add(starts_at, discipline, city)
            expected_all.append((starts_at, event_id))
            if discipline == "Running" and city == "Bogotá":
                expected_combined.append((starts_at, event_id))
        for _ in range(5):
            self.add(START + timedelta(hours=1), "Running", "Bogotá", status="DRAFT")

        everything = self.pages(7)
        combined = self.pages(4, discipline="RUNNING", city="bogota")

        self.assertEqual(everything, [event_id for _, event_id in sorted(expected_all)])
        self.assertEqual(len(set(everything)), 150)
        self.assertEqual(combined, [event_id for _, event_id in sorted(expected_combined)])


if __name__ == "__main__":
    unittest.main()
