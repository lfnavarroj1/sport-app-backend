"""HU014 contra PostgreSQL real: catálogo precargado, transacción del plan, idempotencia
concurrente, autorización, Usuarios caído, outbox, proyección, reconstrucción y consulta
keyset (requiere `TEST_DATABASE_URL`)."""

import importlib
import threading
import unittest
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DataError
from support import (
    NutritionAuthorOnly,
    StaticRestrictions,
    TrainingDatabases,
    athlete_and_professional,
    nutrition_command,
    training_databases,
)

from training_management.application.commands.create_nutrition_plan import CreateNutritionPlan
from training_management.application.errors import (
    AthleteAuthorizationRequired,
    NutritionRestrictionsUnavailable,
)
from training_management.application.messages import parse_training_event
from training_management.application.ports import OutboxMessage
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.application.queries.nutrition_plans import (
    GetNutritionPlan,
    ListMyNutritionPlans,
    NutritionPlanFilters,
)
from training_management.domain.nutrition.catalog import FOOD_CATALOG
from training_management.domain.nutrition.model import InvalidNutritionPlan, NutritionRestrictions
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.persistence_read.history import (
    SqlAlchemyAuthorizationReader,
)
from training_management.infrastructure.persistence_read.models import (
    NutritionPlanViewRow,
    ProcessedEventRow,
)
from training_management.infrastructure.persistence_write.food_catalog import (
    FoodCatalogReadinessCheck,
    SqlAlchemyFoodCatalogReader,
)
from training_management.infrastructure.persistence_write.models import (
    FoodCatalogItemRow,
    IdempotencyKeyRow,
    NutritionPlanItemRow,
    NutritionPlanMealRow,
    NutritionPlanRow,
    OutboxEventRow,
)
from training_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from training_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyNutritionUnitOfWork,
)
from training_management.workers.outbox_publisher import build_envelope

seed_0006 = importlib.import_module(
    "training_management.infrastructure.persistence_write.migrations.versions.0006_nutrition_plans"
)
CONCURRENCY = 8
WRITE_MODELS = (
    NutritionPlanRow,
    NutritionPlanMealRow,
    NutritionPlanItemRow,
    IdempotencyKeyRow,
)


class BrokenOutboxUnitOfWork(SqlAlchemyNutritionUnitOfWork):
    def add_outbox(self, message: OutboxMessage) -> None:
        super().add_outbox(OutboxMessage(**{**message.__dict__, "aggregate_type": "x" * 65}))


def run_concurrently(task: Callable[[], Any], times: int = CONCURRENCY) -> list[Any]:
    barrier = threading.Barrier(times)
    results: list[Any] = [None] * times

    def worker(index: int) -> None:
        barrier.wait()
        try:
            results[index] = task()
        except Exception as error:
            results[index] = error

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(times)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


class FoodCatalogPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: TrainingDatabases = self.enterContext(training_databases("hu014_catalog"))

    def test_migration_preloads_the_expected_catalog(self) -> None:
        loaded = SqlAlchemyFoodCatalogReader(self.db.writes).load()

        self.assertEqual(loaded.catalog_version, FOOD_CATALOG.catalog_version)
        self.assertEqual(
            sorted(loaded.items, key=lambda item: item.food_code),
            sorted(FOOD_CATALOG.items, key=lambda item: item.food_code),
        )
        with self.db.writes() as session:
            labels = set(session.scalars(select(FoodCatalogItemRow.label_key)))
        self.assertEqual(labels, {item.label_key for item in FOOD_CATALOG.items})
        self.assertTrue(FoodCatalogReadinessCheck(self.db.write_engine)())

    def test_seed_is_idempotent_and_restores_values(self) -> None:
        with self.db.write_engine.begin() as connection:
            connection.execute(
                update(FoodCatalogItemRow)
                .where(FoodCatalogItemRow.food_code == "PASTA")
                .values(allergens=[])
            )
        with self.db.write_engine.begin() as connection:
            seed_0006.seed_food_catalog(connection)
            seed_0006.seed_food_catalog(connection)

        self.assertEqual(self.db.count(FoodCatalogItemRow), len(FOOD_CATALOG.items))
        pasta = SqlAlchemyFoodCatalogReader(self.db.writes).load().find("PASTA")
        assert pasta is not None
        self.assertEqual(pasta.allergens, ("WHEAT",))

    def test_readiness_fails_with_missing_food_or_other_version(self) -> None:
        check = FoodCatalogReadinessCheck(self.db.write_engine)
        with self.db.write_engine.begin() as connection:
            connection.execute(
                delete(FoodCatalogItemRow).where(FoodCatalogItemRow.food_code == "WATER")
            )
        with self.assertLogs("sportapp.catalog", level="WARNING") as missing:
            self.assertFalse(check())

        with self.db.write_engine.begin() as connection:
            connection.execute(update(FoodCatalogItemRow).values(catalog_version="otra.v1"))
        with self.assertLogs("sportapp.catalog", level="WARNING") as other_version:
            self.assertFalse(check())

        self.assertEqual(missing.records[0].__dict__["fields"]["problem"], "food_missing")
        self.assertEqual(
            other_version.records[0].__dict__["fields"]["problem"], "catalog_version_missing"
        )


class NutritionPlanPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: TrainingDatabases = self.enterContext(training_databases("hu014"))
        self.athlete, self.professional = athlete_and_professional()
        self.restrictions = StaticRestrictions()
        self.create = self.db.create_nutrition_plan(self.restrictions)

    def authorize(self) -> None:
        self.db.grant()(self.athlete, self.professional.user_id)
        self.db.discard_pending_outbox()  # evento de autorización (HU011), ajeno a estas pruebas

    def counts(self) -> list[int]:
        """Filas del plan, comidas, ítems, eventos del plan (sin los de autorización) y claves."""
        with self.db.writes() as session:
            events = session.scalar(
                select(func.count()).where(OutboxEventRow.aggregate_type == "nutrition_plan")
            )
        return [
            *(self.db.count(model) for model in WRITE_MODELS),
            events or 0,
        ]

    def published(self) -> list[dict[str, Any]]:
        store = SqlAlchemyOutboxStore(self.db.writes)
        claimed = store.claim_batch("w-test", 100, 30)
        for event in claimed:
            store.mark_published(event.event_id, "w-test")
        return [build_envelope(event) for event in claimed]

    def test_plan_meals_items_outbox_and_key_commit_together(self) -> None:
        """CA1, CA4 y CA5 en una sola transacción local; cantidades `NUMERIC(5, 1)`."""
        self.authorize()
        command = nutrition_command(self.athlete.user_id)

        result = self.create(self.professional, command)

        with self.db.writes() as session:
            plan = session.get(NutritionPlanRow, result.plan.plan_id)
            meals = list(
                session.scalars(
                    select(NutritionPlanMealRow).order_by(NutritionPlanMealRow.position)
                )
            )
            items = list(
                session.scalars(
                    select(NutritionPlanItemRow).order_by(
                        NutritionPlanItemRow.meal_position, NutritionPlanItemRow.position
                    )
                )
            )
            key = session.get(IdempotencyKeyRow, command.idempotency_key)
        assert plan is not None
        assert key is not None
        self.assertEqual((plan.status, plan.version), ("ASSIGNED", 1))
        self.assertEqual(plan.catalog_version, "food-catalog-2026-10-13.v1")
        self.assertEqual([meal.meal_type for meal in meals], ["BREAKFAST", "LUNCH"])
        self.assertEqual(
            [(i.meal_position, i.position, i.food_code, i.quantity, i.unit) for i in items],
            [
                (0, 0, "OATS", Decimal("80.0"), "G"),
                (0, 1, "MILK", Decimal("200.0"), "ML"),
                (1, 0, "CHICKEN_BREAST", Decimal("150.5"), "G"),
            ],
        )
        self.assertEqual((key.operation, key.resource_id), ("create_nutrition_plan", plan.plan_id))
        self.assertEqual(key.response["warnings"], ["nutrition_habits_missing"])
        self.assertEqual(result.warnings, ("nutrition_habits_missing",))
        [envelope] = self.published()
        self.assertEqual(
            (envelope["event_type"], envelope["aggregate_type"], envelope["aggregate_id"]),
            ("plan.alimentacion_creado.v1", "nutrition_plan", str(plan.plan_id)),
        )
        self.assertEqual(envelope["causation_id"], str(command.idempotency_key))
        self.assertNotIn("warnings", envelope["payload"])

    def test_outbox_failure_leaves_nothing(self) -> None:
        self.authorize()
        create = CreateNutritionPlan(
            unit_of_work=lambda: BrokenOutboxUnitOfWork(self.db.writes),
            permissions=NutritionAuthorOnly(),
            catalog=SqlAlchemyFoodCatalogReader(self.db.writes),
            restrictions=self.restrictions,
            clock=SystemClock(),
        )

        with self.assertRaises(DataError):
            create(self.professional, nutrition_command(self.athlete.user_id))

        self.assertEqual(self.counts(), [0, 0, 0, 0, 0])

    def test_eight_concurrent_retries_create_one_plan_and_event(self) -> None:
        """CA1: ocho reintentos simultáneos con la misma clave → un plan y un evento."""
        self.authorize()
        command = nutrition_command(self.athlete.user_id)

        results = run_concurrently(lambda: self.create(self.professional, command))

        self.assertEqual([r for r in results if isinstance(r, Exception)], [])
        self.assertEqual({r.plan.plan_id for r in results}, {results[0].plan.plan_id})
        self.assertEqual(sum(not r.replayed for r in results), 1)
        self.assertEqual(self.counts(), [1, 2, 3, 1, 1])

    def test_revoked_authorization_is_rejected_without_reading_restrictions(self) -> None:
        """CA3: sin autorización o con la autorización revocada → 403, nada escrito."""
        with self.assertRaises(AthleteAuthorizationRequired):
            self.create(self.professional, nutrition_command(self.athlete.user_id))
        self.authorize()
        self.db.revoke()(self.athlete, self.professional.user_id)

        with self.assertRaises(AthleteAuthorizationRequired):
            self.create(self.professional, nutrition_command(self.athlete.user_id))

        self.assertEqual(self.restrictions.calls, [])
        self.assertEqual(self.counts(), [0, 0, 0, 0, 0])

    def test_users_unavailable_or_conflicts_create_nothing(self) -> None:
        """CA2 y CA6: Usuarios caído → 503 sin plan; alergia o restricción → 422 sin plan."""
        self.authorize()
        self.restrictions.unavailable = True
        with self.assertRaises(NutritionRestrictionsUnavailable):
            self.create(self.professional, nutrition_command(self.athlete.user_id))
        self.restrictions.unavailable = False
        self.restrictions.result = NutritionRestrictions(("GLUTEN_FREE",), ("MILK",), "VEGAN", 2)
        with self.assertRaises(InvalidNutritionPlan) as blocked:
            self.create(self.professional, nutrition_command(self.athlete.user_id))

        self.assertEqual(
            [(item.field, item.issue) for item in blocked.exception.violations],
            [
                ("meals.0.items.1.food_code", "conflicts_with_allergy"),
                ("meals.0.items.1.food_code", "conflicts_with_restriction"),
                ("meals.1.items.0.food_code", "conflicts_with_restriction"),
            ],
        )
        self.assertEqual(self.counts(), [0, 0, 0, 0, 0])


class NutritionProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: TrainingDatabases = self.enterContext(training_databases("hu014_cqrs"))
        self.athlete, self.professional = athlete_and_professional()
        self.db.grant()(self.athlete, self.professional.user_id)
        self.create = self.db.create_nutrition_plan(
            StaticRestrictions(NutritionRestrictions((), (), "OMNIVORE", 1))
        )
        self.project = self.db.project()

    def publish_and_project(self) -> list[Any]:
        store = SqlAlchemyOutboxStore(self.db.writes)
        claimed = store.claim_batch("w-test", 100, 30)
        for event in claimed:
            store.mark_published(event.event_id, "w-test")
        events = [parse_training_event(build_envelope(event)) for event in claimed]
        return [self.project(event) for event in events]

    def test_projection_duplicates_and_queries(self) -> None:
        """CA5 y CA3 con persistencia real: proyección, duplicados y acceso por actor."""
        plan = self.create(self.professional, nutrition_command(self.athlete.user_id)).plan
        store = SqlAlchemyOutboxStore(self.db.writes)
        claimed = store.claim_batch("w-test", 100, 30)
        envelopes = [build_envelope(event) for event in claimed]
        nutrition = next(e for e in envelopes if e["event_type"] == "plan.alimentacion_creado.v1")
        for envelope in envelopes:
            if envelope is not nutrition:
                self.project(parse_training_event(envelope))
        event = parse_training_event(nutrition)

        outcomes = run_concurrently(lambda: self.project(event), times=4)

        self.assertEqual(
            sorted(outcome.value for outcome in outcomes),
            ["duplicate", "duplicate", "duplicate", "projected"],
        )
        self.assertEqual(self.db.count(NutritionPlanViewRow, read=True), 1)
        reader = self.db.nutrition_reader()
        self.assertEqual(reader.get(plan.plan_id), plan)
        get = GetNutritionPlan(
            reader=reader,
            authorizations=SqlAlchemyAuthorizationReader(self.db.reads),
            permissions=NutritionAuthorOnly(),
        )
        self.assertEqual(get(self.athlete, plan.plan_id), plan)
        self.assertEqual(get(self.professional, plan.plan_id), plan)

        self.db.revoke()(self.athlete, self.professional.user_id)
        self.publish_and_project()
        with self.assertRaises(AthleteAuthorizationRequired):
            get(self.professional, plan.plan_id)

    def test_rebuild_on_empty_read_model(self) -> None:
        """Reconstrucción: repetir los eventos sobre una lectura vacía da la misma vista."""
        plans = [
            self.create(self.professional, nutrition_command(self.athlete.user_id)).plan
            for _ in range(2)
        ]
        with self.db.writes() as session:
            payloads = [
                row.payload
                for row in session.scalars(
                    select(OutboxEventRow).where(OutboxEventRow.aggregate_type == "nutrition_plan")
                )
            ]
        self.publish_and_project()
        first = {plan.plan_id: self.db.nutrition_reader().get(plan.plan_id) for plan in plans}
        with self.db.reads() as session, session.begin():
            session.execute(delete(NutritionPlanViewRow))
            session.execute(delete(ProcessedEventRow))
        with self.db.writes() as session, session.begin():
            session.execute(update(OutboxEventRow).values(published_at=None))

        outcomes = self.publish_and_project()

        self.assertIn(ProjectionOutcome.PROJECTED, outcomes)
        rebuilt = {plan.plan_id: self.db.nutrition_reader().get(plan.plan_id) for plan in plans}
        self.assertEqual(rebuilt, first)
        self.assertEqual(rebuilt, {plan.plan_id: plan for plan in plans})
        self.assertEqual(len(payloads), 2)
        with self.db.reads() as session:
            projected = session.scalar(
                select(func.count())
                .select_from(ProcessedEventRow)
                .where(ProcessedEventRow.event_type == "plan.alimentacion_creado.v1")
            )
        self.assertEqual(projected, 2)

    def test_my_plans_keyset_pagination(self) -> None:
        plans = [
            self.create(self.professional, nutrition_command(self.athlete.user_id)).plan
            for _ in range(5)
        ]
        self.publish_and_project()
        mine = ListMyNutritionPlans(reader=self.db.nutrition_reader())

        seen: list[Any] = []
        cursor = None
        while True:
            page = mine(self.athlete, NutritionPlanFilters(limit=2, cursor=cursor))
            seen.extend(page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        late = self.create(self.professional, nutrition_command(self.athlete.user_id)).plan
        self.publish_and_project()
        after_last = mine(self.athlete, NutritionPlanFilters(limit=50, cursor=cursor))

        expected = sorted(plans, key=lambda plan: (plan.created_at, plan.plan_id), reverse=True)
        self.assertEqual(seen, expected)
        self.assertEqual(after_last.next_cursor, None)
        self.assertNotIn(late, after_last.items)
        self.assertEqual(mine(self.athlete, NutritionPlanFilters()).items[0].plan_id, late.plan_id)
        self.assertEqual(
            ListMyNutritionPlans(reader=self.db.nutrition_reader())(
                athlete_and_professional()[0], NutritionPlanFilters()
            ).items,
            (),
        )


if __name__ == "__main__":
    unittest.main()
