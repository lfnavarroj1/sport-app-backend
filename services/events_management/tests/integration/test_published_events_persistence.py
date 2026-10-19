"""HU035 en Eventos contra PostgreSQL real: superposición en los bordes, exclusión de
borradores y keyset sin pérdidas ni repeticiones (ADR-031).

Los eventos `PUBLISHED` se insertan directamente en `event_read`: la publicación pertenece
a HU024, que todavía no existe.
"""

import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from support import event_databases

from events_management.application.ports import Caller
from events_management.application.queries.published_events import (
    EventsFilters,
    ListPublishedEvents,
)
from events_management.infrastructure.persistence_read.events import (
    SqlAlchemyEventReader,
    SqlAlchemyPublishedEventReader,
)
from events_management.infrastructure.persistence_read.models import EventReadRow

NOV_1 = datetime(2026, 11, 1, 5, 0, tzinfo=UTC)
NOV_30 = datetime(2026, 12, 1, 4, 59, 59, tzinfo=UTC)


class Granted:
    def has_capability(self, user_id: UUID, capability: str) -> bool:
        return capability == "calendar.sport.view"


class PublishedEventsPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = self.enterContext(event_databases("hu035_events"))
        self.athlete = Caller(uuid4(), "athlete")
        self.list = ListPublishedEvents(
            reader=SqlAlchemyPublishedEventReader(self.db.reads), capabilities=Granted()
        )

    def add(self, starts_at: datetime, hours: int = 2, status: str = "PUBLISHED") -> UUID:
        event_id = uuid4()
        with self.db.reads() as session, session.begin():
            session.add(
                EventReadRow(
                    event_id=event_id,
                    organizer_id=uuid4(),
                    name="Carrera 10K Bogotá",
                    description="Carrera recreativa de diez kilómetros para aficionados.",
                    discipline="Running",
                    location_name="Parque Metropolitano Simón Bolívar",
                    location_address="Avenida Calle 63 # 60-50",
                    location_city="Bogotá",
                    location_country_code="CO",
                    starts_at=starts_at,
                    ends_at=starts_at + timedelta(hours=hours),
                    time_zone="America/Bogota",
                    duration_minutes=hours * 60,
                    capacity=500,
                    status=status,
                    version=2 if status == "PUBLISHED" else 1,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                    projected_at=datetime.now(UTC),
                )
            )
        return event_id

    def test_overlap_boundaries_and_drafts(self) -> None:
        """CA1 y CA5: incluye los que tocan los bordes; excluye los de fuera y los borradores."""
        ends_at_from = self.add(NOV_1 - timedelta(hours=2))  # termina justo en `from`
        starts_at_to = self.add(NOV_30)  # empieza justo en `to`
        inside = self.add(datetime(2026, 11, 15, 12, tzinfo=UTC))
        self.add(NOV_1 - timedelta(hours=3))  # termina 1 h antes de `from`
        self.add(NOV_30 + timedelta(seconds=1))
        draft = self.add(datetime(2026, 11, 16, 12, tzinfo=UTC), status="DRAFT")

        page = self.list(self.athlete, EventsFilters(NOV_1, NOV_30))

        self.assertEqual(
            [item.event_id for item in page.items], [ends_at_from, inside, starts_at_to]
        )
        self.assertNotIn(draft, [item.event_id for item in page.items])
        self.assertEqual(
            SqlAlchemyEventReader(self.db.reads).public_events()[0].status, "PUBLISHED"
        )

    def test_keyset_with_ties_does_not_lose_or_repeat(self) -> None:
        same_start = datetime(2026, 11, 10, 12, tzinfo=UTC)
        created = {self.add(same_start) for _ in range(5)} | {
            self.add(same_start + timedelta(days=day)) for day in range(1, 4)
        }
        seen: list[UUID] = []
        cursor: str | None = None
        while True:
            page = self.list(self.athlete, EventsFilters(NOV_1, NOV_30, 3, cursor))
            seen.extend(item.event_id for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break

        self.assertEqual(len(seen), len(created))
        self.assertEqual(set(seen), created)


if __name__ == "__main__":
    unittest.main()
