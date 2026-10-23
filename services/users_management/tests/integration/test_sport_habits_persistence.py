"""HU006 (ADR-034) contra PostgreSQL real: hábitos, outbox e idempotencia en una transacción,
control optimista bajo concurrencia, migración `0005_sport_habits` y proyección en
`athlete_profile_view.sport_habits` (requiere `TEST_DATABASE_URL`)."""

import json
import unittest
from typing import Any
from uuid import UUID, uuid4

from projection_support import ReadDatabase, profile_envelope, read_database
from sqlalchemy import create_engine, insert, inspect, pool, select
from sqlalchemy.exc import IntegrityError
from support import WRITE_HISTORY, WriteDatabase, write_database
from test_profile_persistence import CONCURRENT_REQUESTS, run_concurrently

from sportapp_platform.migrations import current_revision, downgrade, upgrade
from sportapp_platform.testing.postgres import temporary_database
from users_management.api.profile_payload import parse_sport_habits
from users_management.application.commands.profile_section import (
    ProfileSectionCommand,
    ProfileSectionResult,
)
from users_management.application.messages import parse_users_event
from users_management.application.projections.users_projection import ProjectionOutcome
from users_management.domain.profile.events import ProfileSection
from users_management.domain.profile.section import VersionConflict
from users_management.domain.profile.validation import InvalidProfileSection
from users_management.infrastructure.persistence_write.models import (
    IdempotencyKeyRow,
    OutboxEventRow,
    SportHabitsRow,
)
from users_management.infrastructure.persistence_write.outbox import ClaimedEvent
from users_management.workers.outbox_publisher import build_envelope

HABITS = ProfileSection.SPORT_HABITS


def habits(key: UUID | None = None, **overrides: Any) -> ProfileSectionCommand[Any]:
    body: dict[str, Any] = {
        "sports": [
            {
                "sport": "swimming",
                "frequency_per_week": 2,
                "session_duration_minutes": 60,
                "intensity": "LOW",
                "level": "BEGINNER",
            },
            {
                "sport": "running",
                "frequency_per_week": 3,
                "session_duration_minutes": 45,
                "intensity": "MODERATE",
                "level": "INTERMEDIATE",
                "years_practicing": 4,
            },
        ],
        "availability": {"days": ["SAT", "MON"], "time_slots": ["MORNING"]},
        "primary_goal": "HEALTH",
        **overrides,
    }
    return ProfileSectionCommand(parse_sport_habits(body), key or uuid4(), uuid4())


class SportHabitsPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._context = write_database("users_management_habits")
        self.db: WriteDatabase = self._context.__enter__()
        self.addCleanup(self._context.__exit__, None, None, None)

    def rows(self) -> list[SportHabitsRow]:
        with self.db.sessions() as session:
            return list(session.scalars(select(SportHabitsRow)))

    def habit_events(self) -> list[OutboxEventRow]:
        with self.db.sessions() as session:
            return list(
                session.scalars(
                    select(OutboxEventRow)
                    .where(OutboxEventRow.aggregate_type == "sport_habits")
                    .order_by(OutboxEventRow.aggregate_version)
                )
            )

    def test_create_and_replace_with_version(self) -> None:
        """CA1 y CA3: alta y reemplazo con versión, estado normalizado y un evento por cambio."""
        user_id = self.db.athlete()
        update = self.db.update_sport_habits()

        first = update(user_id, habits())
        second = update(user_id, habits(expected_version=1, primary_goal=None))

        self.assertEqual((first.status, second.status), (201, 200))
        [row] = self.rows()
        self.assertEqual((row.version, row.primary_goal), (2, None))
        self.assertEqual([sport["sport"] for sport in row.sports], ["running", "swimming"])
        self.assertEqual(row.availability, {"days": ["MON", "SAT"], "time_slots": ["MORNING"]})
        self.assertEqual(second.document["created_at"], first.document["created_at"])
        events = self.habit_events()
        self.assertEqual([e.aggregate_version for e in events], [1, 2])
        self.assertEqual({e.event_type for e in events}, {"habitos.deportivos_actualizados.v1"})
        self.assertEqual(events[-1].payload, second.document)
        self.assertTrue(all(e.aggregate_id == user_id for e in events))

    def test_rejections_leave_no_section_event_or_key(self) -> None:
        """CA2: `422` todo o nada y conflicto de versión sin datos parciales."""
        user_id = self.db.athlete()
        update = self.db.update_sport_habits()
        key = uuid4()

        with self.assertRaises(InvalidProfileSection):
            update(user_id, habits(key, primary_goal="FUN"))
        with self.assertRaises(VersionConflict):
            update(user_id, habits(expected_version=1))

        self.assertEqual(self.rows(), [])
        self.assertEqual(self.habit_events(), [])
        with self.db.sessions() as session:
            keys = session.scalars(
                select(IdempotencyKeyRow.operation).where(
                    IdempotencyKeyRow.operation == "update_sport_habits"
                )
            ).all()
        self.assertEqual(keys, [], "el registro del deportista tiene su propia clave")

    def test_eight_concurrent_replacements_create_one_version(self) -> None:
        """Ocho reemplazos con la misma `expected_version` → una versión y siete `409`."""
        user_id = self.db.athlete()
        update = self.db.update_sport_habits()
        update(user_id, habits())
        goals = ["PERFORMANCE", "RECREATION", "COMPETITION", "WEIGHT_MANAGEMENT"]

        outcomes = run_concurrently(
            [
                lambda index=index: update(
                    user_id,
                    habits(expected_version=1, primary_goal=goals[index % len(goals)]),
                )
                for index in range(CONCURRENT_REQUESTS)
            ]
        )

        succeeded = [item for item in outcomes if isinstance(item, ProfileSectionResult)]
        conflicts = [item for item in outcomes if isinstance(item, VersionConflict)]
        self.assertEqual((len(succeeded), len(conflicts)), (1, CONCURRENT_REQUESTS - 1), outcomes)
        self.assertTrue(all(conflict.current_version == 2 for conflict in conflicts))
        [row] = self.rows()
        self.assertEqual(
            (row.version, row.primary_goal), (2, succeeded[0].document["primary_goal"])
        )
        self.assertEqual([e.aggregate_version for e in self.habit_events()], [1, 2])

    def test_concurrent_retries_with_same_key_produce_one_effect(self) -> None:
        """CA4: reintentos simultáneos con la misma clave → mismo resultado y un evento."""
        user_id = self.db.athlete()
        update = self.db.update_sport_habits()
        key = uuid4()

        outcomes = run_concurrently(
            [lambda: update(user_id, habits(key)) for _ in range(CONCURRENT_REQUESTS)]
        )

        self.assertTrue(all(isinstance(item, ProfileSectionResult) for item in outcomes), outcomes)
        self.assertEqual(sum(not item.replayed for item in outcomes), 1)
        self.assertEqual(len(self.habit_events()), 1)

    def test_athletes_are_isolated(self) -> None:
        """CA5: cada deportista tiene su propia sección y su propia versión."""
        ana, luis = self.db.athlete(), self.db.athlete()
        update = self.db.update_sport_habits()

        update(ana, habits())
        update(luis, habits(primary_goal="COMPETITION"))
        update(ana, habits(expected_version=1, primary_goal="RECREATION"))

        by_user = {row.user_id: (row.version, row.primary_goal) for row in self.rows()}
        self.assertEqual(by_user, {ana: (2, "RECREATION"), luis: (1, "COMPETITION")})

    def test_database_rejects_out_of_contract_shapes(self) -> None:
        user_id = self.db.athlete()
        valid: dict[str, Any] = {
            "user_id": user_id,
            "version": 1,
            "sports": [{"sport": "running"}],
            "availability": {"days": ["MON"], "time_slots": ["MORNING"]},
            "primary_goal": None,
            "created_at": "2026-10-23T12:00:00Z",
            "updated_at": "2026-10-23T12:00:00Z",
        }
        for overrides in (
            {"version": 0},
            {"sports": []},
            {"sports": [{"sport": "running"}] * 6},
            {"sports": {"sport": "running"}},
            {"availability": ["MON"]},
            {"primary_goal": "FUN"},
            {"user_id": uuid4()},  # FK local a `user_account`
        ):
            with self.subTest(overrides=list(overrides)), self.db.engine.connect() as connection:
                with self.assertRaises(IntegrityError):
                    connection.execute(insert(SportHabitsRow).values(**{**valid, **overrides}))


class SportHabitsMigrationTest(unittest.TestCase):
    def test_downgrade_removes_the_table_and_upgrade_restores_it(self) -> None:
        """`0005` es reversible: quita solo su tabla y se puede volver a aplicar."""
        with temporary_database("users_management_0005") as url:
            upgrade(WRITE_HISTORY, url)

            downgrade(WRITE_HISTORY, url, "0004_athlete_profile")

            engine = create_engine(url, poolclass=pool.NullPool)
            try:
                tables = set(inspect(engine).get_table_names())
            finally:
                engine.dispose()
            self.assertEqual(current_revision(url), "0004_athlete_profile")
            self.assertNotIn("sport_habits", tables)
            self.assertIn("demographic_profile", tables)

            upgrade(WRITE_HISTORY, url)

            self.assertEqual(current_revision(url), "0005_sport_habits")


class SportHabitsProjectionTest(unittest.TestCase):
    """CQRS con la base de lectura real: duplicados, versión menor, fila ausente y
    reconstrucción; y el sobre que publica la outbox llega a la vista."""

    def setUp(self) -> None:
        self._read = read_database("users_management_habits_read")
        self.read: ReadDatabase = self._read.__enter__()
        self.addCleanup(self._read.__exit__, None, None, None)
        self.project = self.read.projector()

    def apply(self, envelope: dict[str, Any]) -> ProjectionOutcome:
        return self.project(parse_users_event(json.loads(json.dumps(envelope))))

    def test_missing_row_duplicate_and_lower_version(self) -> None:
        user_id = uuid4()
        newest = profile_envelope(HABITS, user_id, 3)

        outcomes = [
            self.apply(newest),
            self.apply(newest),
            self.apply(profile_envelope(HABITS, user_id, 2)),
        ]

        self.assertEqual(
            outcomes,
            [ProjectionOutcome.APPLIED, ProjectionOutcome.DUPLICATE, ProjectionOutcome.OBSOLETE],
        )
        [row] = self.read.profile_rows()
        expected = {k: v for k, v in newest["payload"].items() if k != "user_id"}
        self.assertEqual((row["sport_habits"], row["versions"]), (expected, (None, None, 3)))

    def test_rebuild_reaches_the_same_view(self) -> None:
        user_id = uuid4()
        events = [profile_envelope(HABITS, user_id, version) for version in (1, 2, 3)]
        for event in events:
            self.apply(event)
        expected = self.read.profile_rows()

        with read_database("users_management_habits_rebuild") as rebuilt:
            project = rebuilt.projector()
            for event in reversed(events):
                project(parse_users_event(json.loads(json.dumps(event))))
            self.assertEqual(rebuilt.profile_rows(), expected)

    def test_command_event_reaches_the_profile_view(self) -> None:
        """CA1 y CA3: el sobre que publicaría la outbox se proyecta y lo lee la consulta."""
        with write_database("users_management_habits_flow") as db:
            user_id = db.athlete()
            created = db.update_sport_habits()(user_id, habits())
            with db.sessions() as session:
                [event] = session.scalars(
                    select(OutboxEventRow).where(OutboxEventRow.aggregate_type == "sport_habits")
                )
        envelope = _envelope_of(event)

        self.assertEqual(self.apply(envelope), ProjectionOutcome.APPLIED)
        view = self.read.reader().get(user_id)
        assert view is not None
        self.assertEqual(view.sport_habits_version, 1)
        self.assertEqual(
            view.sport_habits, {k: v for k, v in created.document.items() if k != "user_id"}
        )


def _envelope_of(event: OutboxEventRow) -> dict[str, Any]:
    """Sobre que publicaría `usuarios-outbox-publisher` para la fila de la outbox."""
    return build_envelope(
        ClaimedEvent(
            event_id=event.event_id,
            event_type=event.event_type,
            schema_version=event.schema_version,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            aggregate_version=event.aggregate_version,
            occurred_at=event.occurred_at,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            payload=event.payload,
            attempts=0,
        )
    )


if __name__ == "__main__":
    unittest.main()
