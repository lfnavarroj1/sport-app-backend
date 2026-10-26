"""HU028 (ADR-041) contra PostgreSQL real (requiere `TEST_DATABASE_URL`): plantilla, outbox y
clave en una transacción; carreras con la misma clave; retiro con la fila bloqueada; outbox →
proyector → `training_plan_template_view`; orden SQL igual al del dominio; retirada no sugerida;
ajena `404`; 200 plantillas con p95 < 2 s (HA010) y migraciones `0008` / `0010`."""

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
from training_management.application.commands.training_plan_templates import (
    PublishTemplate,
    PublishTemplateCommand,
    RetireTemplate,
    RetireTemplateCommand,
)
from training_management.application.messages import parse_training_event
from training_management.application.ports import Caller, TemplateSuggestionQuery
from training_management.application.queries.training_plan_templates import (
    ListMyTemplates,
    MyTemplatesFilters,
    SuggestTemplates,
    TemplateSuggestionFilters,
)
from training_management.domain.plans.model import Sport
from training_management.domain.templates.model import (
    DayOfWeek,
    Goal,
    Level,
    TemplateInput,
    TemplateNotFound,
    TemplateStatus,
    TemplateVersionConflict,
    TrainingPlanTemplate,
    WeeklySession,
)
from training_management.domain.templates.suggestions import (
    TemplateSuggestionContext,
    personalized_order,
)
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.persistence_read.models import TrainingPlanTemplateViewRow
from training_management.infrastructure.persistence_read.templates import (
    SqlAlchemyTemplateReader,
    template_view_values,
)
from training_management.infrastructure.persistence_write.models import (
    CommandIdempotencyRow,
    OutboxEventRow,
    TrainingPlanTemplateRow,
)
from training_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from training_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyTemplateUnitOfWork,
)
from training_management.workers.outbox_publisher import build_envelope

P95_LIMIT_SECONDS = 2.0


class PlanAuthor:
    def allows(self, actor_type: str, permission: str) -> bool:
        return actor_type == "service_provider" and permission == "training.plan.author"


class SuggestCapable:
    def has_capability(self, user_id: UUID, capability: str) -> bool:
        return capability == "training.plans.suggest"


def template_input(**overrides: Any) -> TemplateInput:
    values: dict[str, Any] = {
        "title": "Base de 10 km",
        "objectives": "Construir base aeróbica en ocho semanas.",
        "sport": Sport.RUNNING,
        "goal": Goal.PERFORMANCE,
        "level": Level.BEGINNER,
        "duration_weeks": 8,
        "weekly_sessions": (
            WeeklySession(DayOfWeek.MON, "Rodaje suave", 40),
            WeeklySession(DayOfWeek.THU, "Series", 45),
        ),
    }
    values.update(overrides)
    return TemplateInput(**values)


class TemplatePersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: TrainingDatabases = self.enterContext(training_databases("hu028"))
        self.professional = Caller(uuid4(), "service_provider")
        self.publish = PublishTemplate(
            unit_of_work=lambda: SqlAlchemyTemplateUnitOfWork(self.db.writes),
            permissions=PlanAuthor(),
            clock=SystemClock(),
        )
        self.retire = RetireTemplate(
            unit_of_work=lambda: SqlAlchemyTemplateUnitOfWork(self.db.writes),
            permissions=PlanAuthor(),
            clock=SystemClock(),
        )
        self.reader = SqlAlchemyTemplateReader(self.db.reads)

    def counts(self) -> tuple[int, int, int]:
        return (
            self.db.count(TrainingPlanTemplateRow),
            self.db.count(OutboxEventRow),
            self.db.count(CommandIdempotencyRow),
        )

    def project_outbox(self) -> None:
        store, project = SqlAlchemyOutboxStore(self.db.writes), self.db.project()
        for event in store.claim_batch("w-test", 100, 30):
            project(parse_training_event(build_envelope(event)))
            store.mark_published(event.event_id, "w-test")

    def test_template_outbox_and_key_commit_together_and_replay(self) -> None:
        """CA4: `PUBLISHED` v1 con sesiones, outbox y clave; el reintento no duplica."""
        key = uuid4()
        first = self.publish(self.professional, PublishTemplateCommand(template_input(), key))
        again = self.publish(self.professional, PublishTemplateCommand(template_input(), key))

        self.assertTrue(again.replayed)
        self.assertEqual(again.template, first.template)
        self.assertEqual(self.counts(), (1, 1, 1))
        with self.db.writes() as session:
            row = session.get(TrainingPlanTemplateRow, first.template.template_id)
            assert row is not None
            self.assertEqual(
                (row.status, row.version, len(row.weekly_sessions)), ("PUBLISHED", 1, 2)
            )
            [event] = session.scalars(select(OutboxEventRow))
            self.assertEqual(event.event_type, "plantilla_entrenamiento.publicada.v1")

    def test_concurrent_requests_with_the_same_key_publish_once(self) -> None:
        key = uuid4()
        results = run_concurrently(
            lambda: self.publish(self.professional, PublishTemplateCommand(template_input(), key))
        )

        self.assertTrue(all(not isinstance(result, Exception) for result in results), results)
        self.assertEqual(len({result.template.template_id for result in results}), 1)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_retire_locks_rejects_foreign_and_stale(self) -> None:
        """CA3: ajena → no encontrada; versión vieja → conflicto; concurrencia → 1 evento."""
        template = self.publish(
            self.professional, PublishTemplateCommand(template_input(), uuid4())
        ).template
        with self.assertRaises(TemplateNotFound):
            self.retire(
                Caller(uuid4(), "service_provider"), RetireTemplateCommand(template.template_id, 1)
            )
        with self.assertRaises(TemplateVersionConflict):
            self.retire(self.professional, RetireTemplateCommand(template.template_id, 2))

        results = run_concurrently(
            lambda: self.retire(self.professional, RetireTemplateCommand(template.template_id, 1)),
            times=4,
        )

        self.assertTrue(all(not isinstance(result, Exception) for result in results), results)
        self.assertEqual({result.template.version for result in results}, {2})
        self.assertEqual(self.counts()[1], 2)

    def test_projection_suggests_and_retired_template_is_not_suggested(self) -> None:
        """CA1 y CA3: outbox → proyector → vista; la retirada conserva su fila."""
        match = self.publish(
            self.professional,
            PublishTemplateCommand(template_input(title="Objetivo", goal=Goal.HEALTH), uuid4()),
        ).template
        other = self.publish(
            self.professional, PublishTemplateCommand(template_input(title="Otra"), uuid4())
        ).template
        retired = self.publish(
            self.professional,
            PublishTemplateCommand(template_input(title="Retirada", goal=Goal.HEALTH), uuid4()),
        ).template
        self.retire(self.professional, RetireTemplateCommand(retired.template_id, 1))
        self.project_outbox()

        result = SuggestTemplates(reader=self.reader, capabilities=SuggestCapable())(
            Caller(uuid4(), "athlete"),
            TemplateSuggestionFilters(sports=(Sport.RUNNING,), primary_goal=Goal.HEALTH),
        )

        self.assertEqual(
            [(item.template.template_id, [r.value for r in item.reasons]) for item in result.items],
            [
                (match.template_id, ["SPORT_MATCH", "GOAL_MATCH"]),
                (other.template_id, ["SPORT_MATCH"]),
            ],
        )
        mine = ListMyTemplates(reader=self.reader, permissions=PlanAuthor())(
            self.professional, MyTemplatesFilters(limit=10)
        )
        self.assertEqual(
            [item.template_id for item in mine.items],
            [retired.template_id, other.template_id, match.template_id],
        )
        self.assertEqual(mine.items[0].status, TemplateStatus.RETIRED)


class TemplateSuggestionVolumeTest(unittest.TestCase):
    """HA010: 200 plantillas; orden SQL = orden del dominio; p95 < 2 s."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._context = training_databases("hu028_volume")
        cls.db = cls._context.__enter__()
        generator = random.Random(28)  # noqa: S311  # nosec B311 - datos sintéticos
        base = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        cls.templates: list[TrainingPlanTemplate] = []
        for index in range(200):
            template = TrainingPlanTemplate.publish(
                template_id=uuid4(),
                professional_id=uuid4(),
                data=template_input(
                    title=f"Plantilla {index}",
                    sport=generator.choice(list(Sport)),
                    goal=generator.choice(list(Goal)),
                ),
                # Algunas comparten instante para ejercitar el desempate por `template_id`.
                now=base + timedelta(minutes=index // 2),
            )
            if index % 10 == 0:
                template = template.retire(base + timedelta(days=1))
            cls.templates.append(template)
        with cls.db.reads() as session, session.begin():
            projected_at = datetime.now(UTC)
            session.execute(
                insert(TrainingPlanTemplateViewRow),
                [template_view_values(item, projected_at) for item in cls.templates],
            )
        cls.reader = SqlAlchemyTemplateReader(cls.db.reads)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._context.__exit__(None, None, None)

    def expected(self, context: TemplateSuggestionContext, limit: int) -> list[UUID]:
        published = [t for t in self.templates if t.status is TemplateStatus.PUBLISHED]
        if context.personalized:
            published = [t for t in published if t.sport in context.sports]
        published.sort(
            key=lambda t: personalized_order(
                context, goal=t.goal, published_at=t.published_at, template_id=t.template_id
            )
        )
        return [t.template_id for t in published[:limit]]

    def test_sql_order_matches_the_domain_order(self) -> None:
        for context in (
            TemplateSuggestionContext((Sport.RUNNING, Sport.CYCLING), Goal.HEALTH),
            TemplateSuggestionContext((Sport.SWIMMING,)),
            TemplateSuggestionContext(()),
        ):
            with self.subTest(context=context):
                found = self.reader.suggest(TemplateSuggestionQuery(context=context, limit=50))

                self.assertEqual([t.template_id for t in found], self.expected(context, 50))

    def test_p95_below_two_seconds_with_200_templates(self) -> None:
        suggest = SuggestTemplates(reader=self.reader, capabilities=SuggestCapable())
        athlete = Caller(uuid4(), "athlete")
        filters = (
            TemplateSuggestionFilters(
                sports=(Sport.RUNNING, Sport.STRENGTH), primary_goal=Goal.PERFORMANCE, limit=50
            ),
            TemplateSuggestionFilters(limit=50),
        )
        timings: list[float] = []
        for index in range(60):
            started = time.perf_counter()
            suggest(athlete, filters[index % 2])
            timings.append(time.perf_counter() - started)
        p95 = statistics.quantiles(timings, n=20)[-1]

        self.assertLess(p95, P95_LIMIT_SECONDS, f"p95 = {p95 * 1000:.1f} ms")


class TemplateMigrationTest(unittest.TestCase):
    def test_write_and_read_migrations_upgrade_and_revert(self) -> None:
        for history, head, previous, table in (
            (
                WRITE_HISTORY,
                "0008_training_plan_templates",
                "0007_routes",
                "training_plan_template",
            ),
            (
                READ_HISTORY,
                "0010_training_plan_template_view",
                "0009_route_view",
                "training_plan_template_view",
            ),
        ):
            with self.subTest(table=table), temporary_database(f"hu028_{table}"[:40]) as url:
                upgrade(history, url, head)
                self.assertEqual(current_revision(url), head)
                self.assertTrue(_indexes(url, table))

                downgrade(history, url, previous)

                self.assertNotIn(table, _tables(url))
                upgrade(history, url, head)
                self.assertEqual(current_revision(url), head)


if __name__ == "__main__":
    unittest.main()
