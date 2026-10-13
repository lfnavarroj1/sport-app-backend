"""HU035 en Entrenamientos contra PostgreSQL real: proyección de sesiones planificadas,
consulta propia con fechas inclusivas y cursor, y backfill de la migración de lectura."""

import unittest
from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import create_engine, pool, select, text
from support import (
    TrainingDatabases,
    athlete_and_professional,
    plan_command,
    training_databases,
)

from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.postgres import temporary_database
from training_management.application.messages import parse_training_event
from training_management.application.ports import Caller, PlanView
from training_management.application.queries.planned_sessions import (
    GetPlannedSessions,
    PlannedSessionFilters,
)
from training_management.domain.plans.events import planned_session_id
from training_management.infrastructure.persistence_read.history import (
    SqlAlchemyPlannedSessionReader,
)
from training_management.infrastructure.persistence_read.models import TrainingPlanSessionViewRow
from training_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from training_management.workers.outbox_publisher import build_envelope

READ_HISTORY = "training_management.infrastructure.persistence_read:migrations"


class PlannedSessionsPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: TrainingDatabases = self.enterContext(training_databases("hu035"))

    def project_outbox(self) -> None:
        store = SqlAlchemyOutboxStore(self.db.writes)
        project = self.db.project()
        for event in store.claim_batch("w-test", 100, 30):
            project(parse_training_event(build_envelope(event)))
            store.mark_published(event.event_id, "w-test")

    def assign_plan(self, athlete: Caller, professional: Caller) -> PlanView:
        self.db.grant()(athlete, professional.user_id)
        return self.db.create_plan()(professional, plan_command(athlete.user_id)).plan

    def test_projected_sessions_are_listed_for_their_athlete_only(self) -> None:
        athlete, professional = athlete_and_professional()
        other, _ = athlete_and_professional()
        plan = self.assign_plan(athlete, professional)
        self.assign_plan(other, professional)
        self.project_outbox()
        self.project_outbox()  # una segunda pasada no duplica (outbox ya publicada)
        query = GetPlannedSessions(reader=SqlAlchemyPlannedSessionReader(self.db.reads))

        page = query.own(athlete, PlannedSessionFilters(date(2026, 11, 2), date(2026, 11, 4)))
        first_day = query.own(athlete, PlannedSessionFilters(date(2026, 11, 2), date(2026, 11, 2)))

        self.assertEqual(
            [item.scheduled_date for item in page.items], [date(2026, 11, 2), date(2026, 11, 4)]
        )
        self.assertEqual(
            [item.session_id for item in page.items],
            [planned_session_id(plan.plan_id, 0), planned_session_id(plan.plan_id, 1)],
        )
        self.assertTrue(all(item.athlete_id == athlete.user_id for item in page.items))
        self.assertEqual(len(first_day.items), 1, "fechas inclusivas")
        self.assertEqual(self.db.count(TrainingPlanSessionViewRow, read=True), 4)

    def test_cursor_walks_every_session_once(self) -> None:
        athlete, professional = athlete_and_professional()
        for _ in range(3):
            self.assign_plan(athlete, professional)
        self.project_outbox()
        query = GetPlannedSessions(reader=SqlAlchemyPlannedSessionReader(self.db.reads))
        seen: list[object] = []
        cursor: str | None = None
        while True:
            page = query.own(
                athlete, PlannedSessionFilters(date(2026, 11, 1), date(2026, 11, 30), 2, cursor)
            )
            seen.extend(item.session_id for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break

        self.assertEqual(len(seen), 6)
        self.assertEqual(len(set(seen)), 6)


class PlannedSessionBackfillTest(unittest.TestCase):
    def test_migration_copies_existing_plans_with_projector_ids(self) -> None:
        plan_id, athlete_id = uuid4(), uuid4()
        with temporary_database("hu035_backfill") as url:
            upgrade(READ_HISTORY, url, "0004_active_training_session")
            engine = create_engine(url, poolclass=pool.NullPool)
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO training_plan_view (plan_id, athlete_id, professional_id, "
                            "title, objectives, start_date, end_date, status, version, sessions, "
                            "created_at, projected_at) VALUES (:p, :a, :pro, 'Base', '[]', "
                            "'2026-11-02', '2026-11-29', 'ASSIGNED', 1, CAST(:s AS jsonb), "
                            ":now, :now)"
                        ),
                        {
                            "p": plan_id,
                            "a": athlete_id,
                            "pro": uuid4(),
                            "s": '[{"scheduled_date": "2026-11-02", "sport": "running", '
                            '"duration_minutes": 40, "intensity_rpe": 5}, '
                            '{"scheduled_date": "2026-11-04", "sport": "strength", '
                            '"duration_minutes": 30, "intensity_rpe": 6}]',
                            "now": datetime.now(UTC),
                        },
                    )
                upgrade(READ_HISTORY, url)
                with engine.connect() as connection:
                    rows = connection.execute(
                        select(
                            TrainingPlanSessionViewRow.session_id,
                            TrainingPlanSessionViewRow.scheduled_date,
                            TrainingPlanSessionViewRow.athlete_id,
                        ).order_by(TrainingPlanSessionViewRow.position)
                    ).all()
            finally:
                engine.dispose()

        self.assertEqual(
            [(row.session_id, row.scheduled_date, row.athlete_id) for row in rows],
            [
                (planned_session_id(plan_id, 0), date(2026, 11, 2), athlete_id),
                (planned_session_id(plan_id, 1), date(2026, 11, 4), athlete_id),
            ],
        )


if __name__ == "__main__":
    unittest.main()
