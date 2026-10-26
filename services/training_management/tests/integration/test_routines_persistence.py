"""HU030 (ADR-041) contra PostgreSQL real (requiere `TEST_DATABASE_URL`): rutina, outbox y clave
en una transacción con el catálogo de alimentos migrado (`food_code` inexistente rechazado);
carreras con la misma clave; retiro con la fila bloqueada; outbox → proyector →
`nutrition_routine_template_view`; exclusión SQL igual a la del dominio; retirada no sugerida;
200 rutinas con p95 < 2 s (HA010) y migraciones `0014`."""

import random
import statistics
import time
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, select
from support import READ_HISTORY, WRITE_HISTORY, TrainingDatabases, training_databases
from test_routes_persistence import _indexes, _tables, run_concurrently

from sportapp_platform.migrations import current_revision, downgrade, upgrade
from sportapp_platform.testing.postgres import temporary_database
from training_management.application.commands.nutrition_routine_templates import (
    PublishRoutine,
    PublishRoutineCommand,
    RetireRoutine,
    RetireRoutineCommand,
)
from training_management.application.messages import parse_training_event
from training_management.application.ports import Caller, RoutineSuggestionQuery
from training_management.application.queries.nutrition_routine_templates import (
    ListMyRoutines,
    MyRoutinesFilters,
    RoutineSuggestionFilters,
    SuggestRoutines,
)
from training_management.domain.nutrition.catalog import FOOD_CATALOG
from training_management.domain.nutrition.model import MealType
from training_management.domain.plans.errors import Violation
from training_management.domain.plans.model import Sport
from training_management.domain.routines.model import (
    Allergy,
    DietPattern,
    InvalidRoutine,
    NutritionRoutineTemplate,
    RoutineInput,
    RoutineItem,
    RoutineMeal,
    RoutineNotFound,
    RoutineStatus,
    RoutineVersionConflict,
)
from training_management.domain.routines.suggestions import (
    RoutineSuggestionContext,
    is_compatible,
    suggestion_order,
)
from training_management.domain.templates.model import Goal
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.persistence_read.models import (
    NutritionRoutineTemplateViewRow,
)
from training_management.infrastructure.persistence_read.routines import (
    SqlAlchemyRoutineReader,
    routine_view_values,
)
from training_management.infrastructure.persistence_write.food_catalog import (
    SqlAlchemyFoodCatalogReader,
)
from training_management.infrastructure.persistence_write.models import (
    CommandIdempotencyRow,
    NutritionRoutineTemplateRow,
    OutboxEventRow,
)
from training_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from training_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyRoutineUnitOfWork,
)
from training_management.workers.outbox_publisher import build_envelope

P95_LIMIT_SECONDS = 2.0
# Alimentos compatibles con cualquier patrón y sin alérgenos en el catálogo.
NEUTRAL_FOODS = ("OATS", "QUINOA", "BERRIES", "SPINACH", "BROCCOLI", "CARROT", "TOMATO")


class NutritionAuthor:
    def allows(self, actor_type: str, permission: str) -> bool:
        return actor_type == "service_provider" and permission == "nutrition.plan.author"


class SuggestCapable:
    def has_capability(self, user_id: UUID, capability: str) -> bool:
        return capability == "nutrition.routines.suggest"


def meals(*codes: str) -> tuple[RoutineMeal, ...]:
    return (RoutineMeal(MealType.LUNCH, tuple(RoutineItem(code, 1) for code in codes)),)


def routine_input(**overrides: Any) -> RoutineInput:
    values: dict[str, Any] = {
        "title": "Desayunos de fondo",
        "description": "Rutina para semanas de carga aeróbica.",
        "goal": Goal.PERFORMANCE,
        "sport": Sport.RUNNING,
        "dietary_pattern": DietPattern.VEGETARIAN,
        "contains_allergens": (),
        "duration_days": 14,
        "daily_meals": (
            RoutineMeal(MealType.BREAKFAST, (RoutineItem("OATS", 1), RoutineItem("BANANA", 1))),
            RoutineMeal(MealType.LUNCH, (RoutineItem("LENTILS", 2),)),
        ),
    }
    values.update(overrides)
    return RoutineInput(**values)


class RoutinePersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: TrainingDatabases = self.enterContext(training_databases("hu030"))
        self.professional = Caller(uuid4(), "service_provider")
        self.publish = PublishRoutine(
            unit_of_work=lambda: SqlAlchemyRoutineUnitOfWork(self.db.writes),
            permissions=NutritionAuthor(),
            catalog=SqlAlchemyFoodCatalogReader(self.db.writes),
            clock=SystemClock(),
        )
        self.retire = RetireRoutine(
            unit_of_work=lambda: SqlAlchemyRoutineUnitOfWork(self.db.writes),
            permissions=NutritionAuthor(),
            clock=SystemClock(),
        )
        self.reader = SqlAlchemyRoutineReader(self.db.reads)

    def counts(self) -> tuple[int, int, int]:
        return (
            self.db.count(NutritionRoutineTemplateRow),
            self.db.count(OutboxEventRow),
            self.db.count(CommandIdempotencyRow),
        )

    def project_outbox(self) -> None:
        store, project = SqlAlchemyOutboxStore(self.db.writes), self.db.project()
        for event in store.claim_batch("w-test", 100, 30):
            project(parse_training_event(build_envelope(event)))
            store.mark_published(event.event_id, "w-test")

    def test_routine_outbox_and_key_commit_together_and_replay(self) -> None:
        """CA4: `PUBLISHED` v1 con comidas del catálogo, outbox y clave; el reintento no duplica."""
        key = uuid4()
        first = self.publish(self.professional, PublishRoutineCommand(routine_input(), key))
        again = self.publish(self.professional, PublishRoutineCommand(routine_input(), key))

        self.assertTrue(again.replayed)
        self.assertEqual(again.routine, first.routine)
        self.assertEqual(self.counts(), (1, 1, 1))
        with self.db.writes() as session:
            row = session.get(NutritionRoutineTemplateRow, first.routine.template_id)
            assert row is not None
            self.assertEqual(
                (row.status, row.version, len(row.daily_meals), row.contains_allergens),
                ("PUBLISHED", 1, 2, []),
            )
            [event] = session.scalars(select(OutboxEventRow))
            self.assertEqual(event.event_type, "plantilla_rutina.publicada.v1")

    def test_unknown_food_code_is_rejected_with_the_migrated_catalog(self) -> None:
        """CA4: `food_code` inexistente → `422` (aquí el error del dominio); nada se escribe."""
        with self.assertRaises(InvalidRoutine) as error:
            self.publish(
                self.professional,
                PublishRoutineCommand(routine_input(daily_meals=meals("OATS", "PIZZA")), uuid4()),
            )

        self.assertEqual(
            error.exception.violations,
            (Violation("daily_meals.0.items.1.food_code", "unknown_value"),),
        )
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_concurrent_requests_with_the_same_key_publish_once(self) -> None:
        key = uuid4()
        results = run_concurrently(
            lambda: self.publish(self.professional, PublishRoutineCommand(routine_input(), key))
        )

        self.assertTrue(all(not isinstance(result, Exception) for result in results), results)
        self.assertEqual(len({result.routine.template_id for result in results}), 1)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_retire_locks_rejects_foreign_and_stale(self) -> None:
        """CA4: retiro idempotente; ajena → no encontrada; versión vieja → conflicto."""
        routine = self.publish(
            self.professional, PublishRoutineCommand(routine_input(), uuid4())
        ).routine
        with self.assertRaises(RoutineNotFound):
            self.retire(
                Caller(uuid4(), "service_provider"), RetireRoutineCommand(routine.template_id, 1)
            )
        with self.assertRaises(RoutineVersionConflict):
            self.retire(self.professional, RetireRoutineCommand(routine.template_id, 2))

        results = run_concurrently(
            lambda: self.retire(self.professional, RetireRoutineCommand(routine.template_id, 1)),
            times=4,
        )

        self.assertTrue(all(not isinstance(result, Exception) for result in results), results)
        self.assertEqual({result.routine.version for result in results}, {2})
        self.assertEqual(self.counts()[1], 2)

    def test_projection_excludes_allergens_and_patterns_and_hides_retired(self) -> None:
        """CA1, CA2 y CA4: outbox → proyector → vista; la retirada conserva su fila."""

        def publish(title: str, **overrides: Any) -> NutritionRoutineTemplate:
            command = PublishRoutineCommand(routine_input(title=title, **overrides), uuid4())
            return self.publish(self.professional, command).routine

        match = publish("Objetivo", goal=Goal.HEALTH)
        other = publish("Otra", sport=None)
        fish = publish(
            "Atún", dietary_pattern=DietPattern.PESCATARIAN, daily_meals=meals("TUNA", "QUINOA")
        )
        meat = publish("Carne", dietary_pattern=DietPattern.OMNIVORE, daily_meals=meals("BEEF"))
        retired = publish("Retirada", goal=Goal.HEALTH)
        self.retire(self.professional, RetireRoutineCommand(retired.template_id, 1))
        self.project_outbox()

        suggest = SuggestRoutines(reader=self.reader, capabilities=SuggestCapable())
        athlete = Caller(uuid4(), "athlete")
        result = suggest(
            athlete,
            RoutineSuggestionFilters(
                dietary_pattern=DietPattern.PESCATARIAN,
                exclude_allergens=(Allergy.FISH,),
                sports=(Sport.RUNNING,),
                primary_goal=Goal.HEALTH,
            ),
        )

        self.assertEqual(
            [(item.routine.template_id, [r.value for r in item.reasons]) for item in result.items],
            [
                (match.template_id, ["RESTRICTION_COMPATIBLE", "GOAL_MATCH", "SPORT_MATCH"]),
                (other.template_id, ["RESTRICTION_COMPATIBLE"]),
            ],
        )
        unpersonalized = suggest(athlete, RoutineSuggestionFilters())
        self.assertFalse(unpersonalized.personalized)
        self.assertEqual(
            [item.routine.template_id for item in unpersonalized.items],
            [meat.template_id, fish.template_id, other.template_id, match.template_id],
        )
        mine = ListMyRoutines(reader=self.reader, permissions=NutritionAuthor())(
            self.professional, MyRoutinesFilters(limit=10)
        )
        self.assertEqual(mine.items[0].template_id, retired.template_id)
        self.assertEqual(mine.items[0].status, RoutineStatus.RETIRED)
        self.assertEqual(len(mine.items), 5)


class RoutineSuggestionVolumeTest(unittest.TestCase):
    """HA010: 200 rutinas; exclusión y orden SQL = los del dominio; p95 < 2 s."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._context = training_databases("hu030_volume")
        cls.db = cls._context.__enter__()
        generator = random.Random(30)  # noqa: S311  # nosec B311 - datos sintéticos
        base = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        cls.routines: list[NutritionRoutineTemplate] = []
        for index in range(200):
            routine = NutritionRoutineTemplate.publish(
                template_id=uuid4(),
                professional_id=uuid4(),
                data=routine_input(
                    title=f"Rutina {index}",
                    goal=generator.choice(list(Goal)),
                    sport=generator.choice([*Sport, None]),
                    dietary_pattern=generator.choice(list(DietPattern)),
                    contains_allergens=tuple(
                        generator.sample(list(Allergy), generator.randint(0, 3))
                    ),
                    daily_meals=meals(*generator.sample(NEUTRAL_FOODS, 3)),
                ),
                catalog=FOOD_CATALOG,
                # Algunas comparten instante para ejercitar el desempate por `template_id`.
                now=base + timedelta(minutes=index // 2),
            )
            if index % 10 == 0:
                routine = routine.retire(base + timedelta(days=1))
            cls.routines.append(routine)
        with cls.db.reads() as session, session.begin():
            projected_at = datetime.now(UTC)
            session.execute(
                insert(NutritionRoutineTemplateViewRow),
                [routine_view_values(item, projected_at) for item in cls.routines],
            )
        cls.reader = SqlAlchemyRoutineReader(cls.db.reads)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._context.__exit__(None, None, None)

    def expected(self, context: RoutineSuggestionContext, limit: int) -> list[UUID]:
        published = [
            r
            for r in self.routines
            if r.status is RoutineStatus.PUBLISHED
            and is_compatible(
                context, dietary_pattern=r.dietary_pattern, contains_allergens=r.contains_allergens
            )
        ]
        published.sort(
            key=lambda r: suggestion_order(
                context,
                goal=r.goal,
                sport=r.sport,
                published_at=r.published_at,
                template_id=r.template_id,
            )
        )
        return [r.template_id for r in published[:limit]]

    def test_sql_exclusion_and_order_match_the_domain(self) -> None:
        for context in (
            RoutineSuggestionContext(
                dietary_pattern=DietPattern.VEGETARIAN,
                exclude_allergens=(Allergy.MILK, Allergy.PEANUTS),
                sports=(Sport.RUNNING, Sport.CYCLING),
                primary_goal=Goal.HEALTH,
            ),
            RoutineSuggestionContext(dietary_pattern=DietPattern.OMNIVORE),
            RoutineSuggestionContext(
                dietary_pattern=DietPattern.VEGAN, exclude_allergens=(Allergy.SOY,)
            ),
            RoutineSuggestionContext(sports=(Sport.SWIMMING,)),
        ):
            with self.subTest(context=context):
                found = self.reader.suggest(RoutineSuggestionQuery(context=context, limit=50))

                self.assertEqual([r.template_id for r in found], self.expected(context, 50))

    def test_p95_below_two_seconds_with_200_routines(self) -> None:
        suggest = SuggestRoutines(reader=self.reader, capabilities=SuggestCapable())
        athlete = Caller(uuid4(), "athlete")
        filters = (
            RoutineSuggestionFilters(
                dietary_pattern=DietPattern.PESCATARIAN,
                exclude_allergens=(Allergy.EGGS, Allergy.SHELLFISH),
                sports=(Sport.RUNNING, Sport.STRENGTH),
                primary_goal=Goal.PERFORMANCE,
                limit=50,
            ),
            RoutineSuggestionFilters(limit=50),
        )
        timings: list[float] = []
        for index in range(60):
            started = time.perf_counter()
            suggest(athlete, filters[index % 2])
            timings.append(time.perf_counter() - started)
        p95 = statistics.quantiles(timings, n=20)[-1]

        self.assertLess(p95, P95_LIMIT_SECONDS, f"p95 = {p95 * 1000:.1f} ms")


class RoutineMigrationTest(unittest.TestCase):
    def test_write_and_read_migrations_upgrade_and_revert(self) -> None:
        for history, head, previous, table in (
            (
                WRITE_HISTORY,
                "0014_nutrition_routine_templates",
                "0013_health_reports",
                "nutrition_routine_template",
            ),
            (
                READ_HISTORY,
                "0014_routine_template_view",
                "0013_health_report_view",
                "nutrition_routine_template_view",
            ),
        ):
            with self.subTest(table=table), temporary_database(f"hu030_{table}"[:40]) as url:
                upgrade(history, url, head)
                self.assertEqual(current_revision(url), head)
                self.assertTrue(_indexes(url, table))

                downgrade(history, url, previous)

                self.assertNotIn(table, _tables(url))
                upgrade(history, url, head)
                self.assertEqual(current_revision(url), head)


if __name__ == "__main__":
    unittest.main()
