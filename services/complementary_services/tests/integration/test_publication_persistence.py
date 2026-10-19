"""HU019 con PostgreSQL real: publicación con outbox atómica, bloqueo del servicio,
idempotencia por profesional, concurrencia y consultas de servicios propios."""

import threading
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from test_service_persistence import future_body

from complementary_services.api.payload import parse_offer
from complementary_services.application.commands.create_service import (
    CreateService,
    CreateServiceCommand,
)
from complementary_services.application.commands.publish_service import (
    PublishedService,
    PublishService,
    PublishServiceCommand,
)
from complementary_services.application.errors import IdempotencyKeyReused, ServiceNotFound
from complementary_services.application.ports import Caller
from complementary_services.application.queries.my_services import (
    GetMyService,
    ListMyServices,
    MyServicesFilters,
)
from complementary_services.domain.validation import InvalidServiceOffer
from complementary_services.infrastructure.clock import SystemClock
from complementary_services.infrastructure.persistence.models import (
    IdempotencyKeyRow,
    OutboxEventRow,
    ServiceAvailabilityRow,
    ServiceRow,
)
from complementary_services.infrastructure.persistence.services_reader import (
    SqlAlchemyServiceReader,
)
from complementary_services.infrastructure.persistence.unit_of_work import (
    SqlAlchemyCreationUnitOfWork,
    SqlAlchemyPublicationUnitOfWork,
)
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.postgres import temporary_database

HISTORY = "complementary_services.infrastructure.persistence:migrations"


class Providers:
    def allows(self, actor_type: str, permission: str) -> bool:
        return actor_type == "service_provider" and permission in {
            "services.create",
            "services.publish",
        }


def concurrently(count: int, action: Any) -> tuple[list[Any], list[Exception]]:
    barrier = threading.Barrier(count)
    results: list[Any] = []
    errors: list[Exception] = []

    def run() -> None:
        barrier.wait()
        try:
            results.append(action())
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results, errors


class PublicationPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        url = self.enterContext(temporary_database("hu019"))
        upgrade(HISTORY, url)
        engine = create_engine(url, pool_size=10, max_overflow=10)
        self.addCleanup(engine.dispose)
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        self.create = CreateService(
            unit_of_work=lambda: SqlAlchemyCreationUnitOfWork(self.sessions),
            permissions=Providers(),
            clock=SystemClock(),
        )
        self.publish = PublishService(
            unit_of_work=lambda: SqlAlchemyPublicationUnitOfWork(self.sessions),
            permissions=Providers(),
            clock=SystemClock(),
        )
        reader = SqlAlchemyServiceReader(self.sessions)
        self.list = ListMyServices(reader=reader)
        self.get = GetMyService(reader=reader)
        self.provider = Caller(uuid4(), "service_provider")

    def draft(self, caller: Caller | None = None, key: UUID | None = None, **body: Any) -> UUID:
        result = self.create(
            caller or self.provider,
            CreateServiceCommand(
                offer=parse_offer(future_body(**body)), idempotency_key=key or uuid4()
            ),
        )
        return result.service.offer.service_id

    def command(self, service_id: UUID, key: UUID | None = None) -> PublishServiceCommand:
        return PublishServiceCommand(
            service_id=service_id, idempotency_key=key or uuid4(), correlation_id=uuid4()
        )

    def outbox(self) -> list[OutboxEventRow]:
        with self.sessions() as session:
            return list(session.scalars(select(OutboxEventRow)))

    def service(self, service_id: UUID) -> ServiceRow:
        with self.sessions() as session:
            row = session.get(ServiceRow, service_id)
        assert row is not None
        return row

    def test_service_outbox_and_key_are_committed_together(self) -> None:
        """CA1 y CA5: un solo COMMIT con la transición, `servicio.publicado.v1` y la clave."""
        service_id = self.draft()
        command = self.command(service_id)

        result = self.publish(self.provider, command)

        service = self.service(service_id)
        self.assertEqual((service.status, service.version), ("PUBLISHED", 2))
        self.assertIsNotNone(service.published_at)
        self.assertEqual(service.published_at, service.updated_at)
        [event] = self.outbox()
        self.assertEqual(
            (event.event_type, event.schema_version, event.aggregate_type),
            ("servicio.publicado.v1", 1, "service"),
        )
        self.assertEqual((event.aggregate_id, event.aggregate_version), (service_id, 2))
        self.assertEqual(event.causation_id, str(command.idempotency_key))
        self.assertEqual(event.correlation_id, command.correlation_id)
        self.assertIsNone(event.published_at)
        self.assertEqual(event.attempts, 0)
        self.assertEqual(event.next_attempt_at, event.occurred_at)
        published_at = result.publication.published_at.isoformat().replace("+00:00", "Z")
        self.assertEqual(
            event.payload,
            {
                "service_id": str(service_id),
                "provider_id": str(self.provider.user_id),
                "status": "PUBLISHED",
                "published_at": published_at,
            },
        )
        with self.sessions() as session:
            record = session.get(
                IdempotencyKeyRow, (self.provider.user_id, command.idempotency_key)
            )
        assert record is not None
        self.assertEqual((record.operation, record.resource_id), ("publish_service", service_id))
        self.assertEqual(record.response["published_at"], published_at)

    def test_concurrent_publications_with_different_keys(self) -> None:
        """Ocho publicaciones simultáneas: una transición, versión 2 y un solo evento."""
        service_id = self.draft()

        results, errors = concurrently(
            8, lambda: self.publish(self.provider, self.command(service_id))
        )

        self.assertEqual(errors, [])
        published: list[PublishedService] = results
        self.assertEqual(sum(result.transitioned for result in published), 1)
        self.assertEqual({result.publication for result in published}, {published[0].publication})
        self.assertEqual(published[0].publication.version, 2)
        self.assertEqual(self.service(service_id).version, 2)
        self.assertEqual(len(self.outbox()), 1)

    def test_concurrent_retries_with_the_same_key(self) -> None:
        """CA4: ocho reintentos simultáneos con la misma clave reciben la misma respuesta."""
        service_id = self.draft()
        key = uuid4()

        results, errors = concurrently(
            8, lambda: self.publish(self.provider, self.command(service_id, key))
        )

        self.assertEqual(errors, [])
        published: list[PublishedService] = results
        self.assertEqual(len(published), 8)
        self.assertEqual({result.publication for result in published}, {published[0].publication})
        self.assertEqual(sum(not result.replayed for result in published), 1)
        self.assertEqual(len(self.outbox()), 1)

    def test_already_published_with_another_key_emits_nothing(self) -> None:
        service_id = self.draft()
        first = self.publish(self.provider, self.command(service_id))

        again = self.publish(self.provider, self.command(service_id))

        self.assertFalse(again.transitioned)
        self.assertEqual(again.publication, first.publication)
        self.assertEqual(self.service(service_id).version, 2)
        self.assertEqual(len(self.outbox()), 1)

    def test_key_reused_for_another_service_or_from_creation(self) -> None:
        creation_key = uuid4()
        first, second = self.draft(key=creation_key), self.draft()
        key = uuid4()
        self.publish(self.provider, self.command(first, key))

        for reused in (key, creation_key):
            with self.subTest(reused=reused), self.assertRaises(IdempotencyKeyReused):
                self.publish(self.provider, self.command(second, reused))
        self.assertEqual(self.service(second).status, "DRAFT")

    def test_foreign_service_is_not_found_and_untouched(self) -> None:
        """CA3."""
        foreign = self.draft(Caller(uuid4(), "service_provider"))

        with self.assertRaises(ServiceNotFound):
            self.publish(self.provider, self.command(foreign))

        self.assertEqual((self.service(foreign).status, self.outbox()), ("DRAFT", []))
        with self.assertRaises(ServiceNotFound):
            self.get(self.provider, foreign)

    def test_expired_intervals_and_one_future_publishes(self) -> None:
        service_id = self.draft()
        yesterday = datetime.now(UTC) - timedelta(days=1)
        with self.sessions.begin() as session:
            session.execute(
                update(ServiceAvailabilityRow)
                .where(
                    ServiceAvailabilityRow.service_id == service_id,
                    ServiceAvailabilityRow.position == 0,
                )
                .values(starts_at=yesterday, ends_at=yesterday + timedelta(hours=1))
            )

        result = self.publish(self.provider, self.command(service_id))

        self.assertEqual(result.publication.version, 2)
        self.assertEqual(self.service(service_id).status, "PUBLISHED")

    def test_without_future_interval_nothing_is_written(self) -> None:
        """CA2: `availability` `required`; el borrador, la outbox y las claves no cambian."""
        service_id = self.draft(availability=[])
        key = uuid4()

        with self.assertRaises(InvalidServiceOffer) as raised:
            self.publish(self.provider, self.command(service_id, key))

        self.assertEqual(
            [(v.field, v.issue.value) for v in raised.exception.violations],
            [("availability", "required")],
        )
        self.assertEqual((self.service(service_id).status, self.outbox()), ("DRAFT", []))
        with self.sessions() as session:
            self.assertIsNone(session.get(IdempotencyKeyRow, (self.provider.user_id, key)))

    def test_status_and_published_at_must_agree(self) -> None:
        service_id = self.draft()

        with self.assertRaises(IntegrityError), self.sessions.begin() as session:
            session.execute(
                update(ServiceRow)
                .where(ServiceRow.service_id == service_id)
                .values(status="PUBLISHED")
            )

    def test_keyset_pages_without_losses_or_repeats(self) -> None:
        created = [self.draft() for _ in range(7)]
        self.draft(Caller(uuid4(), "service_provider"))  # ajeno: nunca aparece
        self.publish(self.provider, self.command(created[2]))
        # Empates de `updated_at`: el orden se decide por `service_id`.
        tie = datetime.now(UTC) - timedelta(hours=1)
        with self.sessions.begin() as session:
            session.execute(
                update(ServiceRow)
                .where(ServiceRow.service_id.in_(created[3:6]))
                .values(updated_at=tie)
            )
        with self.sessions() as session:
            expected = [
                row.service_id
                for row in session.scalars(
                    select(ServiceRow)
                    .where(ServiceRow.provider_id == self.provider.user_id)
                    .order_by(ServiceRow.updated_at.desc(), ServiceRow.service_id.desc())
                )
            ]

        seen: list[UUID] = []
        cursor = None
        while True:
            page = self.list(self.provider, MyServicesFilters(limit=3, cursor=cursor))
            seen.extend(item.offer.service_id for item in page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        self.assertEqual(seen, expected)
        self.assertEqual(sorted(seen), sorted(created))
        published = self.list(self.provider, MyServicesFilters(status="PUBLISHED"))
        [only] = published.items
        self.assertEqual(only.offer.service_id, created[2])
        self.assertIsNotNone(only.offer.published_at)

    def test_detail_returns_the_full_offer_in_utc(self) -> None:
        service_id = self.draft()

        view = self.get(self.provider, service_id)

        document = view.to_document()
        self.assertEqual(len(document["availability"]), 2)
        self.assertTrue(document["availability"][0]["starts_at"].endswith("+00:00"))
        self.assertEqual(document["price"]["amount"], "120000.50")
        self.assertIsNone(document["published_at"])
        with self.sessions() as session:
            count = session.scalar(select(func.count()).select_from(ServiceRow))
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
