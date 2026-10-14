"""HU020 con PostgreSQL real: catálogo de publicados, límites del rango, filtros y keyset
estable con 57 servicios (ADR-038). Los servicios se insertan de forma sintética: la
publicación ya se prueba en HU019."""

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from complementary_services.application.ports import AvailabilityQuery, CatalogPosition
from complementary_services.domain.catalog.offer import Category, Modality
from complementary_services.infrastructure.persistence.catalog_reader import (
    SqlAlchemyServiceCatalogReader,
)
from complementary_services.infrastructure.persistence.models import (
    ServiceAvailabilityRow,
    ServiceRow,
)
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.postgres import temporary_database

HISTORY = "complementary_services.infrastructure.persistence:migrations"
NOW = datetime.now(UTC).replace(microsecond=0)
FROM = NOW + timedelta(days=1)
TO = FROM + timedelta(days=31)


class AvailabilityPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        url = self.enterContext(temporary_database("hu020"))
        upgrade(HISTORY, url)
        engine = create_engine(url)
        self.addCleanup(engine.dispose)
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        self.reader = SqlAlchemyServiceCatalogReader(self.sessions)

    def add(
        self,
        name: str,
        *,
        starts: tuple[datetime, ...] = (FROM + timedelta(days=2),),
        category: Category = Category.COACHING,
        modality: Modality = Modality.VIRTUAL,
        published: bool = True,
    ) -> UUID:
        service_id = uuid4()
        with self.sessions() as session, session.begin():
            session.add(
                ServiceRow(
                    service_id=service_id,
                    provider_id=uuid4(),
                    name=name,
                    description="Servicio sintético para la consulta de disponibilidad.",
                    category=category.value,
                    modality=modality.value,
                    price_amount=Decimal("85000.50"),
                    currency="COP",
                    billing_unit="HOUR",
                    status="PUBLISHED" if published else "DRAFT",
                    version=2 if published else 1,
                    created_at=NOW,
                    updated_at=NOW,
                    published_at=NOW if published else None,
                )
            )
            session.flush()
            session.add_all(
                ServiceAvailabilityRow(
                    service_id=service_id,
                    position=position,
                    starts_at=starts_at,
                    ends_at=starts_at + timedelta(hours=1),
                    time_zone="America/Bogota",
                    capacity=4,
                )
                for position, starts_at in enumerate(starts)
            )
        return service_id

    def query(self, **overrides: object) -> AvailabilityQuery:
        values: dict[str, object] = {
            "starts_from": FROM,
            "starts_before": TO,
            "now": NOW,
            "categories": None,
            "modalities": None,
            "only_available": True,
            "after": None,
            "limit": 100,
        }
        values.update(overrides)
        return AvailabilityQuery(**values)  # type: ignore[arg-type]

    def test_range_bounds_drafts_and_price(self) -> None:
        """CA1 y CA2: inicio en `[from, to)` y futuro; borradores fuera; tarifa decimal."""
        self.add(
            "Con límites",
            starts=(
                FROM - timedelta(minutes=1),  # antes del rango
                FROM,  # borde incluido
                TO - timedelta(minutes=1),  # dentro
                TO,  # borde excluido
            ),
        )
        self.add("Sin intervalos en el rango", starts=(TO + timedelta(days=1),))
        self.add("Borrador", published=False)
        past = self.add("Ya iniciado", starts=(NOW - timedelta(hours=1),))

        available = self.reader.published_availability(self.query())
        everything = self.reader.published_availability(
            self.query(only_available=False, starts_from=NOW - timedelta(days=1))
        )

        self.assertEqual([offer.name for offer in available], ["Con límites"])
        self.assertEqual(
            [interval.starts_at for interval in available[0].availability],
            [FROM, TO - timedelta(minutes=1)],
        )
        self.assertEqual(str(available[0].price.amount), "85000.50")
        self.assertEqual(
            sorted(offer.name for offer in everything),
            ["Con límites", "Sin intervalos en el rango", "Ya iniciado"],
        )
        started = next(offer for offer in everything if offer.service_id == past)
        self.assertEqual(started.availability, (), "un intervalo ya iniciado no se ofrece")

    def test_filters(self) -> None:
        self.add("Medicina", category=Category.SPORTS_MEDICINE, modality=Modality.IN_PERSON)
        self.add("Coaching virtual")
        self.add("Transporte", category=Category.TRANSPORT, modality=Modality.IN_PERSON)

        names = [
            offer.name
            for offer in self.reader.published_availability(
                self.query(
                    categories=frozenset({Category.SPORTS_MEDICINE, Category.TRANSPORT}),
                    modalities=frozenset({Modality.IN_PERSON}),
                )
            )
        ]

        self.assertEqual(names, ["Medicina", "Transporte"])

    def test_keyset_with_57_services_is_stable(self) -> None:
        """CA4: 57 servicios (incluidos nombres repetidos) sin pérdidas ni repeticiones."""
        created = {self.add(f"Servicio {index % 50:02d}") for index in range(57)}
        seen: list[UUID] = []
        after: CatalogPosition | None = None
        while True:
            page = self.reader.published_availability(self.query(after=after, limit=10))
            seen.extend(offer.service_id for offer in page)
            if len(page) < 10:
                break
            after = CatalogPosition(page[-1].name, page[-1].service_id)

        self.assertEqual(len(seen), 57)
        self.assertEqual(set(seen), created)
        again = [o.service_id for o in self.reader.published_availability(self.query(limit=57))]
        self.assertEqual(seen, again, "el orden por nombre e identificador es estable")


if __name__ == "__main__":
    unittest.main()
