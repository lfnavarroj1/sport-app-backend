"""HU015 con PostgreSQL real: transacción, idempotencia por profesional y concurrencia."""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, func, pool, select
from sqlalchemy.orm import sessionmaker

from complementary_services.api.payload import parse_offer
from complementary_services.application.commands.create_service import (
    CreateService,
    CreateServiceCommand,
)
from complementary_services.application.ports import Caller
from complementary_services.domain.validation import InvalidServiceOffer
from complementary_services.infrastructure.clock import SystemClock
from complementary_services.infrastructure.persistence.models import (
    Base,
    IdempotencyKeyRow,
    ServiceAvailabilityRow,
    ServiceRow,
)
from complementary_services.infrastructure.persistence.unit_of_work import (
    SqlAlchemyCreationUnitOfWork,
)
from sportapp_platform.migrations import current_revision, downgrade, upgrade
from sportapp_platform.testing.postgres import temporary_database

HISTORY = "complementary_services.infrastructure.persistence:migrations"


class ProvidersOnly:
    def allows(self, actor_type: str, permission: str) -> bool:
        return actor_type == "service_provider" and permission == "services.create"


def future_body(**overrides: Any) -> dict[str, Any]:
    """Cuerpo válido con intervalos dentro de 30 días (las pruebas usan la hora real)."""
    day = (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
    values: dict[str, Any] = {
        "name": "Valoración deportiva inicial",
        "description": "Valoración presencial para orientar el plan deportivo.",
        "category": "SPORTS_MEDICINE",
        "modality": "IN_PERSON",
        "price": {"amount": "120000.50", "currency": "COP", "billing_unit": "SESSION"},
        "availability": [
            {
                "starts_at": f"{day}T09:00:00-05:00",
                "ends_at": f"{day}T10:00:00-05:00",
                "time_zone": "America/Bogota",
                "capacity": 3,
            },
            {
                "starts_at": f"{day}T10:00:00-05:00",
                "ends_at": f"{day}T11:00:00-05:00",
                "time_zone": "America/Bogota",
                "capacity": 1,
            },
        ],
    }
    values.update(overrides)
    return values


class ServicePersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        url = self.enterContext(temporary_database("hu015"))
        upgrade(HISTORY, url)
        engine = create_engine(url, pool_size=10, max_overflow=10)
        self.addCleanup(engine.dispose)
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        self.create = CreateService(
            unit_of_work=lambda: SqlAlchemyCreationUnitOfWork(self.sessions),
            permissions=ProvidersOnly(),
            clock=SystemClock(),
        )
        self.provider = Caller(uuid4(), "service_provider")

    def command(self, key: UUID, **body: Any) -> CreateServiceCommand:
        return CreateServiceCommand(offer=parse_offer(future_body(**body)), idempotency_key=key)

    def count(self, model: Any) -> int:
        with self.sessions() as session:
            return session.scalar(select(func.count()).select_from(model)) or 0

    def test_service_intervals_and_key_are_committed_together(self) -> None:
        """CA1, CA6 y CA7 con PostgreSQL: importe exacto e instantes en UTC."""
        key = uuid4()
        result = self.create(self.provider, self.command(key))

        with self.sessions() as session:
            service = session.get(ServiceRow, result.service.offer.service_id)
            intervals = list(
                session.scalars(
                    select(ServiceAvailabilityRow).order_by(ServiceAvailabilityRow.position)
                )
            )
            record = session.get(IdempotencyKeyRow, (self.provider.user_id, key))
        assert service is not None
        assert record is not None
        self.assertEqual((service.status, service.version), ("DRAFT", 1))
        self.assertEqual(service.provider_id, self.provider.user_id)
        self.assertEqual(service.price_amount, Decimal("120000.50"))
        self.assertEqual(len(intervals), 2)
        self.assertEqual(intervals[0].starts_at.utcoffset().total_seconds(), 0)  # type: ignore[union-attr]
        self.assertEqual(intervals[0].starts_at.hour, 14)
        self.assertEqual(intervals[0].time_zone, "America/Bogota")
        self.assertEqual(record.resource_id, service.service_id)

    def test_invalid_offer_leaves_nothing(self) -> None:
        """CA3."""
        with self.assertRaises(InvalidServiceOffer):
            self.create(self.provider, self.command(uuid4(), provider_id=str(uuid4())))

        for model in (ServiceRow, ServiceAvailabilityRow, IdempotencyKeyRow):
            self.assertEqual(self.count(model), 0, model.__tablename__)

    def test_concurrent_retries_create_a_single_service(self) -> None:
        """CA4 con 8 reintentos simultáneos de la misma clave y el mismo profesional."""
        key = uuid4()
        barrier = threading.Barrier(8)

        def attempt(_: int) -> Any:
            barrier.wait()
            try:
                return self.create(self.provider, self.command(key))
            except Exception as error:
                return error

        with ThreadPoolExecutor(max_workers=8) as pool_:
            results = list(pool_.map(attempt, range(8)))

        self.assertEqual([r for r in results if isinstance(r, Exception)], [])
        self.assertEqual(
            {r.service.offer.service_id for r in results}, {results[0].service.offer.service_id}
        )
        self.assertEqual(self.count(ServiceRow), 1)
        self.assertEqual(self.count(ServiceAvailabilityRow), 2)

    def test_same_key_for_two_providers_creates_two_services(self) -> None:
        key = uuid4()
        self.create(self.provider, self.command(key))
        self.create(Caller(uuid4(), "service_provider"), self.command(key))

        self.assertEqual(self.count(ServiceRow), 2)
        self.assertEqual(self.count(IdempotencyKeyRow), 2)

    def test_drafts_are_not_published(self) -> None:
        """CA5: el borrador queda fuera del catálogo público (solo `PUBLISHED`, HU019/HU020)."""
        self.create(self.provider, self.command(uuid4()))

        with self.sessions() as session:
            published = session.scalar(
                select(func.count()).select_from(ServiceRow).where(ServiceRow.status == "PUBLISHED")
            )
        self.assertEqual(published, 0)


class MigrationTest(unittest.TestCase):
    def test_migration_matches_models_and_downgrades(self) -> None:
        with temporary_database("hu015_drift") as url:
            upgrade(HISTORY, url)
            engine = create_engine(url, poolclass=pool.NullPool)
            try:
                with engine.connect() as connection:
                    differences = compare_metadata(
                        MigrationContext.configure(connection), Base.metadata
                    )
            finally:
                engine.dispose()
            self.assertEqual(differences, [])

            downgrade(HISTORY, url, "0001_baseline")
            self.assertEqual(current_revision(url), "0001_baseline")
            upgrade(HISTORY, url)
            self.assertEqual(current_revision(url), "0006_appointments")


if __name__ == "__main__":
    unittest.main()
