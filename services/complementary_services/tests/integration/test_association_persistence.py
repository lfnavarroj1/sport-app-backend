"""HU017 con PostgreSQL real: índice único parcial, concurrencia, idempotencia por
deportista, desasociación con historial y elegibilidad del servicio (ADR-038)."""

import unittest
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from test_publication_persistence import Providers, concurrently
from test_service_persistence import future_body

from complementary_services.api.payload import parse_offer
from complementary_services.application.commands.associate_service import (
    MEDICAL_CAPABILITY,
    AssociatedResult,
    AssociateService,
    AssociateServiceCommand,
    DissociateService,
    MedicalAccess,
)
from complementary_services.application.commands.create_service import (
    CreateService,
    CreateServiceCommand,
)
from complementary_services.application.commands.publish_service import (
    PublishService,
    PublishServiceCommand,
)
from complementary_services.application.errors import (
    AssociationExists,
    AssociationNotFound,
    IdempotencyKeyReused,
    ServiceNotFound,
)
from complementary_services.application.ports import Caller
from complementary_services.application.queries.my_associations import (
    ListMyAssociations,
    MyAssociationsFilters,
)
from complementary_services.infrastructure.clock import SystemClock
from complementary_services.infrastructure.persistence.associations_reader import (
    SqlAlchemyAssociationReader,
)
from complementary_services.infrastructure.persistence.models import (
    IdempotencyKeyRow,
    ServiceAssociationRow,
)
from complementary_services.infrastructure.persistence.unit_of_work import (
    SqlAlchemyAssociationUnitOfWork,
    SqlAlchemyCreationUnitOfWork,
    SqlAlchemyPublicationUnitOfWork,
)
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.postgres import temporary_database

HISTORY = "complementary_services.infrastructure.persistence:migrations"


class MedicalCapability:
    def has_capability(self, user_id: UUID, capability: str) -> bool:
        return capability == MEDICAL_CAPABILITY


class AssociationPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        url = self.enterContext(temporary_database("hu017"))
        upgrade(HISTORY, url)
        engine = create_engine(url, pool_size=10, max_overflow=10)
        self.addCleanup(engine.dispose)
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        clock = SystemClock()
        access = MedicalAccess(MedicalCapability())
        self.associate = AssociateService(
            unit_of_work=lambda: SqlAlchemyAssociationUnitOfWork(self.sessions),
            access=access,
            clock=clock,
        )
        self.dissociate = DissociateService(
            unit_of_work=lambda: SqlAlchemyAssociationUnitOfWork(self.sessions),
            access=access,
            clock=clock,
        )
        self.list = ListMyAssociations(
            reader=SqlAlchemyAssociationReader(self.sessions), access=access
        )
        self.provider = Caller(uuid4(), "service_provider")
        self.athlete = Caller(uuid4(), "athlete")
        self.service_id = self.service()

    def service(self, *, publish: bool = True, **body: Any) -> UUID:
        """Servicio real de HU015, publicado con HU019 salvo que se pida el borrador."""
        created = CreateService(
            unit_of_work=lambda: SqlAlchemyCreationUnitOfWork(self.sessions),
            permissions=Providers(),
            clock=SystemClock(),
        )(
            self.provider,
            CreateServiceCommand(offer=parse_offer(future_body(**body)), idempotency_key=uuid4()),
        )
        service_id = created.service.offer.service_id
        if publish:
            PublishService(
                unit_of_work=lambda: SqlAlchemyPublicationUnitOfWork(self.sessions),
                permissions=Providers(),
                clock=SystemClock(),
            )(self.provider, PublishServiceCommand(service_id=service_id, idempotency_key=uuid4()))
        return service_id

    def command(
        self, service_id: UUID | None = None, key: UUID | None = None
    ) -> AssociateServiceCommand:
        return AssociateServiceCommand(
            service_id=service_id or self.service_id, idempotency_key=key or uuid4()
        )

    def rows(self) -> list[ServiceAssociationRow]:
        with self.sessions() as session:
            return list(session.scalars(select(ServiceAssociationRow)))

    def test_association_and_key_are_committed_together(self) -> None:
        """CA1 y CA4: una fila `ACTIVE` sin datos de salud y la clave del deportista."""
        key = uuid4()

        result = self.associate(self.athlete, self.command(key=key))

        [row] = self.rows()
        self.assertEqual(
            (row.association_id, row.service_id, row.athlete_id, row.status, row.version),
            (
                result.association.association.association_id,
                self.service_id,
                self.athlete.user_id,
                "ACTIVE",
                1,
            ),
        )
        self.assertIsNone(row.ended_at)
        with self.sessions() as session:
            record = session.get(IdempotencyKeyRow, (self.athlete.user_id, key))
        assert record is not None
        self.assertEqual(record.operation, "associate_service")
        self.assertEqual(record.response, result.association.to_document())
        self.assertEqual(
            set(ServiceAssociationRow.__table__.columns.keys()),
            {
                "association_id",
                "service_id",
                "athlete_id",
                "status",
                "associated_at",
                "ended_at",
                "version",
            },
            "CA4: la tabla no guarda datos de salud ni copias de perfiles",
        )

    def test_partial_unique_index_allows_history_but_one_active(self) -> None:
        now = datetime.now(UTC)

        def insert(status: str, ended_at: datetime | None) -> None:
            with self.sessions() as session:
                session.add(
                    ServiceAssociationRow(
                        association_id=uuid4(),
                        service_id=self.service_id,
                        athlete_id=self.athlete.user_id,
                        status=status,
                        associated_at=now,
                        ended_at=ended_at,
                        version=1,
                    )
                )
                session.commit()

        insert("INACTIVE", now)
        insert("INACTIVE", now)
        insert("ACTIVE", None)
        with self.assertRaises(IntegrityError) as raised:
            insert("ACTIVE", None)
        self.assertIn("uq_service_association_active", str(raised.exception))
        with self.assertRaises(IntegrityError):
            insert("ACTIVE", now)  # `ended_at` solo en las inactivas
        with self.assertRaises(IntegrityError):
            insert("INACTIVE", None)
        self.assertEqual(len(self.rows()), 3)

    def test_concurrent_associations_with_different_keys(self) -> None:
        """Consistencia: ocho solicitudes simultáneas dejan una sola `ACTIVE`."""
        results, errors = concurrently(8, lambda: self.associate(self.athlete, self.command()))

        self.assertEqual(len(results), 1, errors)
        winner: AssociatedResult = results[0]
        self.assertEqual(len(errors), 7)
        for error in errors:
            self.assertIsInstance(error, AssociationExists)
            assert isinstance(error, AssociationExists)
            self.assertEqual(error.association_id, winner.association.association.association_id)
        self.assertEqual([row.status for row in self.rows()], ["ACTIVE"])

    def test_concurrent_retries_with_the_same_key(self) -> None:
        """CA3: ocho reintentos simultáneos con la misma clave reciben la misma respuesta."""
        key = uuid4()

        results, errors = concurrently(
            8, lambda: self.associate(self.athlete, self.command(key=key))
        )

        self.assertEqual(errors, [])
        associated: list[AssociatedResult] = results
        self.assertEqual({item.association for item in associated}, {associated[0].association})
        self.assertEqual(sum(not item.replayed for item in associated), 1)
        self.assertEqual(len(self.rows()), 1)

    def test_draft_or_non_medical_service_is_not_found(self) -> None:
        """CA2: servicio no publicado (borrador) o de otra categoría → `service_not_found`."""
        draft = self.service(publish=False)
        coaching = self.service(category="COACHING")

        for service_id in (draft, coaching, uuid4()):
            with self.subTest(service_id=service_id), self.assertRaises(ServiceNotFound):
                self.associate(self.athlete, self.command(service_id))
        self.assertEqual(self.rows(), [])

    def test_key_reused_with_another_service(self) -> None:
        key = uuid4()
        other = self.service(name="Fisioterapia deportiva")
        self.associate(self.athlete, self.command(key=key))

        with self.assertRaises(IdempotencyKeyReused):
            self.associate(self.athlete, self.command(other, key))

    def test_dissociation_keeps_history_and_is_idempotent(self) -> None:
        """CA5: `INACTIVE` con `ended_at`, `204` repetido y evidencia con `ALL`."""
        first = self.associate(self.athlete, self.command())
        association_id = first.association.association.association_id

        results, errors = concurrently(4, lambda: self.dissociate(self.athlete, association_id))
        again = self.associate(self.athlete, self.command())

        self.assertEqual(errors, [])
        self.assertEqual({(item.status, item.version) for item in results}, {("INACTIVE", 2)})
        self.assertEqual(len({item.ended_at for item in results}), 1)
        history = self.list(self.athlete, MyAssociationsFilters(status="ALL")).items
        active = self.list(self.athlete, MyAssociationsFilters()).items
        self.assertEqual(
            [item.association.association_id for item in history],
            [again.association.association.association_id, association_id],
        )
        self.assertEqual([item.association.status for item in history], ["ACTIVE", "INACTIVE"])
        self.assertEqual(history[1].service.name, "Valoración deportiva inicial")
        self.assertEqual(str(history[1].service.fee.amount), "120000.50")
        self.assertEqual(
            [item.association.association_id for item in active],
            [again.association.association.association_id],
        )
        with self.assertRaises(AssociationNotFound):
            self.dissociate(Caller(uuid4(), "athlete"), association_id)

    def test_listing_paginates_with_keyset(self) -> None:
        created = [
            self.associate(
                self.athlete, self.command(self.service(name=f"Servicio médico {index}"))
            )
            for index in range(5)
        ]
        self.associate(Caller(uuid4(), "athlete"), self.command())

        seen: list[UUID] = []
        cursor = None
        while True:
            page = self.list(self.athlete, MyAssociationsFilters(limit=2, cursor=cursor))
            seen += [item.association.association_id for item in page.items]
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        self.assertEqual(
            seen,
            [item.association.association.association_id for item in reversed(created)],
        )
        with self.sessions() as session:
            total = session.scalar(select(func.count()).select_from(ServiceAssociationRow))
        self.assertEqual(total, 6)


if __name__ == "__main__":
    unittest.main()
