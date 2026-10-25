"""HU043 contra PostgreSQL real: transacción de consumo, ítems, outbox e idempotencia, ocho
envíos concurrentes, `intake_id` reutilizado, proyección con duplicados y reconstrucción, y el
catálogo con `ETag`/`304` desde la base migrada (ADR-037, CA1-CA4)."""

import threading
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DataError
from support import TrainingDatabases, training_databases, utc_seconds

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from sportapp_platform.tokens import VerifiedAccessToken
from training_management.api.food_catalog import build_food_catalog_router
from training_management.application.commands.register_food_intake import (
    IntakeOutcome,
    RegisterFoodIntake,
    RegisterFoodIntakeCommand,
)
from training_management.application.errors import IdempotencyKeyReused, IntakeIdReused
from training_management.application.messages import parse_training_event
from training_management.application.ports import Caller, OutboxMessage
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.application.queries.food_catalog import GetFoodCatalog
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.persistence_read.models import FoodIntakeViewRow
from training_management.infrastructure.persistence_write.food_catalog import (
    SqlAlchemyFoodCatalogReader,
)
from training_management.infrastructure.persistence_write.models import (
    CommandIdempotencyRow,
    FoodIntakeItemRow,
    FoodIntakeRow,
    OutboxEventRow,
)
from training_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from training_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyFoodIntakeUnitOfWork,
)
from training_management.workers.outbox_publisher import build_envelope

CATALOG_ETAG = '"food-catalog-2026-10-13.v1"'


class BrokenOutboxUnitOfWork(SqlAlchemyFoodIntakeUnitOfWork):
    def add_outbox(self, message: OutboxMessage) -> None:
        super().add_outbox(OutboxMessage(**{**message.__dict__, "aggregate_type": "x" * 65}))


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


def intake_command(
    *,
    key: UUID | None = None,
    intake_id: UUID | None = None,
    consumed_at: str | None = None,
    meal_type: str = "LUNCH",
    rice: str = "150",
) -> RegisterFoodIntakeCommand:
    return RegisterFoodIntakeCommand(
        body={
            "intake_id": str(intake_id or uuid4()),
            "consumed_at": consumed_at or utc_seconds(-timedelta(hours=1)),
            "meal_type": meal_type,
            "items": [
                {"food_code": "WHITE_RICE", "quantity": rice, "unit": "G"},
                {"food_code": "CHICKEN_BREAST", "quantity": "120.5", "unit": "G"},
                {"food_code": "WATER", "quantity": "1", "unit": "CUP"},
            ],
        },
        idempotency_key=str(key or uuid4()),
        correlation_id=uuid4(),
    )


class FoodIntakePersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: TrainingDatabases = self.enterContext(training_databases("hu043"))
        self.athlete = Caller(uuid4(), "athlete")
        self.register = self.command(SqlAlchemyFoodIntakeUnitOfWork)

    def command(self, unit_of_work: type[SqlAlchemyFoodIntakeUnitOfWork]) -> RegisterFoodIntake:
        return RegisterFoodIntake(
            unit_of_work=lambda: unit_of_work(self.db.writes),
            catalog=SqlAlchemyFoodCatalogReader(self.db.writes),
            clock=SystemClock(),
        )

    def published(self) -> list[dict[str, Any]]:
        store = SqlAlchemyOutboxStore(self.db.writes)
        claimed = store.claim_batch("w-test", 100, 30)
        for event in claimed:
            store.mark_published(event.event_id, "w-test")
        return [build_envelope(event) for event in claimed]

    def counts(self) -> list[int]:
        return [
            self.db.count(model)
            for model in (FoodIntakeRow, FoodIntakeItemRow, OutboxEventRow, CommandIdempotencyRow)
        ]

    def view_rows(self) -> list[tuple[Any, ...]]:
        with self.db.reads() as session:
            return [
                (row.intake_id, row.athlete_id, row.consumed_at, row.meal_type, row.items)
                for row in session.scalars(
                    select(FoodIntakeViewRow).order_by(FoodIntakeViewRow.intake_id)
                )
            ]

    def test_intake_items_event_and_key_commit_together(self) -> None:
        """CA1 y CA4: consumo, ítems con un decimal, evento completo y clave de 30 días."""
        key = uuid4()
        result = self.register(self.athlete, intake_command(key=key))

        intake = result.intake
        with self.db.writes() as session:
            row = session.get(FoodIntakeRow, intake.intake_id)
            items = list(
                session.scalars(select(FoodIntakeItemRow).order_by(FoodIntakeItemRow.position))
            )
            command = session.scalar(select(CommandIdempotencyRow))
        assert row is not None
        assert command is not None
        self.assertEqual(
            (row.athlete_id, row.meal_type, row.version), (self.athlete.user_id, "LUNCH", 1)
        )
        self.assertEqual(row.catalog_version, "food-catalog-2026-10-13.v1")
        self.assertEqual(row.data_fingerprint, intake.data.fingerprint)
        self.assertEqual(
            [(item.food_code, item.quantity, item.unit) for item in items],
            [
                ("WHITE_RICE", Decimal("150.0"), "G"),
                ("CHICKEN_BREAST", Decimal("120.5"), "G"),
                ("WATER", Decimal("1.0"), "CUP"),
            ],
        )
        self.assertEqual((command.operation, command.key), ("register_food_intake", key))
        self.assertEqual(command.expires_at - command.created_at, timedelta(days=30))
        [envelope] = self.published()
        self.assertEqual(
            (envelope["event_type"], envelope["aggregate_type"], envelope["aggregate_version"]),
            ("consumo_alimento.registrado.v1", "food_intake", 1),
        )
        self.assertEqual(envelope["aggregate_id"], str(intake.intake_id))
        self.assertEqual(envelope["causation_id"], str(key))
        self.assertEqual(
            envelope["payload"]["items"][2],
            {"food_code": "WATER", "quantity": "1.0", "unit": "CUP"},
        )

    def test_outbox_failure_leaves_nothing(self) -> None:
        """CA2 / CA4: todo o nada en la misma transacción local."""
        with self.assertRaises(DataError):
            self.command(BrokenOutboxUnitOfWork)(self.athlete, intake_command())

        self.assertEqual(self.counts(), [0, 0, 0, 0])

    def test_eight_identical_concurrent_requests_create_one_intake_and_event(self) -> None:
        """CA3 y CA4: ocho envíos con la misma clave → un consumo y un evento."""
        command = intake_command(key=uuid4())

        results, errors = concurrently(8, lambda: self.register(self.athlete, command))

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(sum(result.outcome is IntakeOutcome.CREATED for result in results), 1)
        self.assertEqual({result.intake for result in results}, {results[0].intake})
        self.assertEqual(self.counts(), [1, 3, 1, 1])

    def test_concurrent_same_intake_id_with_different_keys(self) -> None:
        intake_id, consumed_at = uuid4(), utc_seconds(-timedelta(hours=2))

        results, errors = concurrently(
            6,
            lambda: self.register(
                self.athlete, intake_command(intake_id=intake_id, consumed_at=consumed_at)
            ),
        )

        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(result.outcome.value for result in results), ["created", *["existing"] * 5]
        )
        self.assertEqual(self.counts()[:3], [1, 3, 1])

    def test_intake_id_and_key_reuse(self) -> None:
        """CA3: mismos datos → existente; otros datos u otra persona → `intake_id_reused`."""
        intake_id, key = uuid4(), uuid4()
        consumed_at = utc_seconds(-timedelta(hours=3))
        first = self.register(
            self.athlete, intake_command(key=key, intake_id=intake_id, consumed_at=consumed_at)
        )

        again = self.register(
            self.athlete, intake_command(intake_id=intake_id, consumed_at=consumed_at)
        )
        replay = self.register(
            self.athlete, intake_command(key=key, intake_id=intake_id, consumed_at=consumed_at)
        )
        with self.assertRaises(IntakeIdReused):
            self.register(
                self.athlete,
                intake_command(intake_id=intake_id, consumed_at=consumed_at, rice="151"),
            )
        with self.assertRaises(IntakeIdReused):
            self.register(
                Caller(uuid4(), "athlete"),
                intake_command(intake_id=intake_id, consumed_at=consumed_at),
            )
        with self.assertRaises(IdempotencyKeyReused):
            self.register(self.athlete, intake_command(key=key))

        self.assertEqual((again.outcome, again.intake), (IntakeOutcome.EXISTING, first.intake))
        self.assertEqual((replay.outcome, replay.intake), (IntakeOutcome.REPLAYED, first.intake))
        self.assertEqual(self.counts(), [1, 3, 1, 1])

    def test_projection_applies_once_and_rebuilds(self) -> None:
        """CA4: `food_intake_view` incorpora cada consumo una vez y se reconstruye igual."""
        project = self.db.project()
        for hours in (1, 2):
            self.register(
                self.athlete, intake_command(consumed_at=utc_seconds(-timedelta(hours=hours)))
            )
        envelopes = self.published()

        outcomes = [project(parse_training_event(item)) for item in [*envelopes, *envelopes]]
        snapshot = self.view_rows()

        self.assertEqual(
            outcomes, [ProjectionOutcome.PROJECTED] * 2 + [ProjectionOutcome.DUPLICATE] * 2
        )
        self.assertEqual(len(snapshot), 2)
        self.assertEqual(
            snapshot[0][4][1], {"food_code": "CHICKEN_BREAST", "quantity": "120.5", "unit": "G"}
        )
        republished = {**envelopes[0], "event_id": str(uuid4())}
        self.assertEqual(project(parse_training_event(republished)), ProjectionOutcome.STALE)

        with self.db.reads() as session, session.begin():
            session.execute(text("TRUNCATE food_intake_view, processed_event"))
        for envelope in reversed(envelopes):
            project(parse_training_event(envelope))

        self.assertEqual(self.view_rows(), snapshot)

    def test_history_index_supports_hu013_order(self) -> None:
        with self.db.reads() as session:
            definition = session.scalar(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'ix_food_intake_view_athlete_order'"
                )
            )
        self.assertIn("(athlete_id, consumed_at DESC, intake_id DESC)", definition or "")

    def test_catalog_from_the_migrated_database_honours_etag(self) -> None:
        """El catálogo que valida el consumo sale de la precarga y responde `304` con su
        `ETag` (`food-catalog-2026-10-13.v1`)."""
        caller = VerifiedAccessToken(
            subject=str(self.athlete.user_id),
            actor_type="athlete",
            token_id="t",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        app = create_service_app(
            service_name="training-management",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_food_catalog_router(
                    verify_token=lambda _: caller,
                    get_catalog=GetFoodCatalog(catalog=SqlAlchemyFoodCatalogReader(self.db.writes)),
                )
            ],
        )
        with TestClient(app) as client:
            auth = {"Authorization": "Bearer x"}
            first = client.get("/v1/food-catalog", headers=auth)
            cached = client.get("/v1/food-catalog", headers={**auth, "If-None-Match": CATALOG_ETAG})

        self.assertEqual((first.status_code, first.headers["ETag"]), (200, CATALOG_ETAG))
        self.assertEqual(len(first.json()["items"]), 41)
        self.assertEqual((cached.status_code, cached.content), (304, b""))


if __name__ == "__main__":
    unittest.main()
