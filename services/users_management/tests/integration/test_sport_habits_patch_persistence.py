"""HU008 (ADR-035) contra PostgreSQL real: edición parcial de los hábitos deportivos con
fusión sobre el agregado bloqueado, versión, idempotencia y outbox en una transacción,
ocho `PATCH` concurrentes → uno, y el estado completo del evento en
`athlete_profile_view.sport_habits` (requiere `TEST_DATABASE_URL`)."""

import json
import unittest
from typing import Any
from uuid import UUID, uuid4

from projection_support import ReadDatabase, read_database
from sqlalchemy import select
from support import WriteDatabase, write_database
from test_profile_persistence import CONCURRENT_REQUESTS, run_concurrently
from test_sport_habits_persistence import _envelope_of, habits

from users_management.api.profile_payload import parse_sport_habits_patch
from users_management.application.commands.profile_section import (
    ProfileSectionCommand,
    ProfileSectionResult,
)
from users_management.application.errors import IdempotencyKeyReused
from users_management.application.messages import parse_users_event
from users_management.application.projections.users_projection import ProjectionOutcome
from users_management.domain.profile.section import SectionNotFound, VersionConflict
from users_management.domain.profile.validation import InvalidProfileSection
from users_management.infrastructure.persistence_write.models import (
    IdempotencyKeyRow,
    OutboxEventRow,
    SportHabitsRow,
)

RUNNING_ONLY = [
    {
        "sport": "cycling",
        "frequency_per_week": 4,
        "session_duration_minutes": 90,
        "intensity": "HIGH",
        "level": "ADVANCED",
    }
]


def patch(key: UUID | None = None, **body: Any) -> ProfileSectionCommand[Any]:
    return ProfileSectionCommand(parse_sport_habits_patch(body), key or uuid4(), uuid4())


class SportHabitsPatchPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._context = write_database("users_management_habits_patch")
        self.db: WriteDatabase = self._context.__enter__()
        self.addCleanup(self._context.__exit__, None, None, None)
        self.user_id = self.db.athlete()
        self.patch = self.db.patch_sport_habits()

    def register(self) -> ProfileSectionResult:
        return self.db.update_sport_habits()(self.user_id, habits())

    def row(self) -> SportHabitsRow:
        with self.db.sessions() as session:
            [row] = session.scalars(select(SportHabitsRow))
            return row

    def habit_events(self) -> list[OutboxEventRow]:
        with self.db.sessions() as session:
            return list(
                session.scalars(
                    select(OutboxEventRow)
                    .where(OutboxEventRow.aggregate_type == "sport_habits")
                    .order_by(OutboxEventRow.aggregate_version)
                )
            )

    def test_each_section_replaces_and_the_rest_is_kept(self) -> None:
        """CA1-CA3: cada sección enviada reemplaza a la vigente y las demás se conservan."""
        created = self.register()

        sports = self.patch(self.user_id, patch(expected_version=1, sports=RUNNING_ONLY))
        availability = self.patch(
            self.user_id,
            patch(expected_version=2, availability={"days": ["SUN"], "time_slots": ["NIGHT"]}),
        )
        goal = self.patch(self.user_id, patch(expected_version=3, primary_goal=None))

        self.assertEqual((sports.status, availability.status, goal.status), (200, 200, 200))
        row = self.row()
        self.assertEqual(row.version, 4)
        self.assertEqual([item["sport"] for item in row.sports], ["cycling"])
        self.assertEqual(row.availability, {"days": ["SUN"], "time_slots": ["NIGHT"]})
        self.assertIsNone(row.primary_goal)
        self.assertEqual(sports.document["availability"], created.document["availability"])
        self.assertEqual(sports.document["primary_goal"], "HEALTH")
        self.assertEqual(availability.document["sports"], sports.document["sports"])
        self.assertEqual(goal.document["created_at"], created.document["created_at"])

    def test_event_carries_the_new_version_and_full_state(self) -> None:
        """CA4: el evento existente de HU006 lleva la versión nueva y el estado completo."""
        self.register()

        result = self.patch(self.user_id, patch(expected_version=1, primary_goal="COMPETITION"))

        events = self.habit_events()
        self.assertEqual([event.aggregate_version for event in events], [1, 2])
        self.assertEqual(events[-1].event_type, "habitos.deportivos_actualizados.v1")
        self.assertEqual(events[-1].payload, result.document)
        self.assertEqual(len(events[-1].payload["sports"]), 2)

    def test_missing_section_and_stale_version_leave_nothing(self) -> None:
        """`404 sport_habits_not_found` y `409 version_conflict` sin datos parciales."""
        with self.assertRaises(SectionNotFound) as missing:
            self.patch(self.user_id, patch(expected_version=1, primary_goal="HEALTH"))
        self.assertEqual(missing.exception.code, "sport_habits_not_found")
        self.assertEqual(self.habit_events(), [])

        self.register()
        with self.assertRaises(VersionConflict) as stale:
            self.patch(self.user_id, patch(expected_version=2, primary_goal="COMPETITION"))
        self.assertEqual(stale.exception.current_version, 1)
        with self.assertRaises(InvalidProfileSection):
            self.patch(self.user_id, patch(expected_version=1, primary_goal="FUN"))

        self.assertEqual((self.row().version, self.row().primary_goal), (1, "HEALTH"))
        self.assertEqual(len(self.habit_events()), 1)
        with self.db.sessions() as session:
            keys = session.scalars(
                select(IdempotencyKeyRow.operation).where(
                    IdempotencyKeyRow.operation == "patch_sport_habits"
                )
            ).all()
        self.assertEqual(keys, [])

    def test_eight_concurrent_patches_create_one_version(self) -> None:
        """Ocho `PATCH` con la misma `expected_version` → una versión y siete `409`."""
        self.register()
        goals = ["PERFORMANCE", "RECREATION", "COMPETITION", "WEIGHT_MANAGEMENT"]

        outcomes = run_concurrently(
            [
                lambda index=index: self.patch(
                    self.user_id,
                    patch(expected_version=1, primary_goal=goals[index % len(goals)]),
                )
                for index in range(CONCURRENT_REQUESTS)
            ]
        )

        succeeded = [item for item in outcomes if isinstance(item, ProfileSectionResult)]
        conflicts = [item for item in outcomes if isinstance(item, VersionConflict)]
        self.assertEqual((len(succeeded), len(conflicts)), (1, CONCURRENT_REQUESTS - 1), outcomes)
        self.assertTrue(all(conflict.current_version == 2 for conflict in conflicts))
        row = self.row()
        self.assertEqual(
            (row.version, row.primary_goal), (2, succeeded[0].document["primary_goal"])
        )
        self.assertEqual([event.aggregate_version for event in self.habit_events()], [1, 2])

    def test_retries_with_the_same_key_produce_one_effect(self) -> None:
        """Reintentos simultáneos con la misma clave → mismo resultado y un solo evento;
        la misma clave con otro cuerpo → `IdempotencyKeyReused`."""
        self.register()
        key = uuid4()

        outcomes = run_concurrently(
            [
                lambda: self.patch(
                    self.user_id, patch(key, expected_version=1, sports=RUNNING_ONLY)
                )
                for _ in range(CONCURRENT_REQUESTS)
            ]
        )

        self.assertTrue(all(isinstance(item, ProfileSectionResult) for item in outcomes), outcomes)
        self.assertEqual(sum(not item.replayed for item in outcomes), 1)
        self.assertEqual({item.status for item in outcomes}, {200})
        self.assertEqual(len(self.habit_events()), 2)
        with self.assertRaises(IdempotencyKeyReused):
            self.patch(self.user_id, patch(key, expected_version=1, primary_goal="HEALTH"))


class SportHabitsPatchProjectionTest(unittest.TestCase):
    def test_patch_event_reaches_the_profile_view(self) -> None:
        """CA4: el sobre de la outbox del `PATCH` actualiza `athlete_profile_view` con el
        estado completo y la versión nueva."""
        with read_database("users_management_habits_patch_read") as read:
            reader: ReadDatabase = read
            project = reader.projector()
            with write_database("users_management_habits_patch_flow") as db:
                user_id = db.athlete()
                db.update_sport_habits()(user_id, habits())
                patched = db.patch_sport_habits()(
                    user_id, patch(expected_version=1, sports=RUNNING_ONLY)
                )
                with db.sessions() as session:
                    events = list(
                        session.scalars(
                            select(OutboxEventRow)
                            .where(OutboxEventRow.aggregate_type == "sport_habits")
                            .order_by(OutboxEventRow.aggregate_version)
                        )
                    )

            outcomes = [
                project(parse_users_event(json.loads(json.dumps(_envelope_of(event)))))
                for event in events
            ]

            self.assertEqual(outcomes, [ProjectionOutcome.APPLIED] * 2)
            view = reader.reader().get(user_id)
            assert view is not None
            self.assertEqual(view.sport_habits_version, 2)
            self.assertEqual(
                view.sport_habits, {k: v for k, v in patched.document.items() if k != "user_id"}
            )


if __name__ == "__main__":
    unittest.main()
