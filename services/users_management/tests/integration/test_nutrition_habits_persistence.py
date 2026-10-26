"""HU009 (ADR-035) contra PostgreSQL real: hábitos alimenticios, outbox e idempotencia en una
transacción, control optimista bajo concurrencia, migraciones `0006_nutrition_habits` (escritura)
y `0005_nutrition_habits_view` (lectura), proyección en `athlete_profile_view.nutrition_habits` y
contrato interno de restricciones sobre la base de lectura (requiere `TEST_DATABASE_URL`)."""

import json
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from projection_support import ReadDatabase, profile_envelope, read_database, registered_envelope
from sqlalchemy import create_engine, insert, inspect, pool, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from support import HEALTH_CONSENT, WRITE_HISTORY, WriteDatabase, write_database
from test_profile_persistence import CONCURRENT_REQUESTS, run_concurrently
from test_sport_habits_persistence import _envelope_of

from sportapp_platform.migrations import current_revision, downgrade, upgrade
from sportapp_platform.testing.postgres import temporary_database
from users_management.api.profile_payload import parse_nutrition_habits
from users_management.application.commands.profile_section import (
    ProfileSectionCommand,
    ProfileSectionResult,
)
from users_management.application.messages import parse_users_event
from users_management.application.projections.users_projection import ProjectionOutcome
from users_management.domain.profile.events import ProfileSection
from users_management.domain.profile.section import VersionConflict
from users_management.domain.profile.validation import InvalidProfileSection
from users_management.infrastructure.persistence_read.models import UserDirectoryRow
from users_management.infrastructure.persistence_read.nutrition_restrictions import NO_HABITS
from users_management.infrastructure.persistence_write.models import (
    IdempotencyKeyRow,
    NutritionHabitsRow,
    OutboxEventRow,
)

HABITS = ProfileSection.NUTRITION_HABITS
READ_HISTORY = "users_management.infrastructure.persistence_read:migrations"


def habits(key: UUID | None = None, **overrides: Any) -> ProfileSectionCommand[Any]:
    body: dict[str, Any] = {
        "consent": {"version": HEALTH_CONSENT},
        "diet_pattern": "VEGETARIAN",
        "restrictions": ["LOW_SUGAR", "GLUTEN_FREE"],
        "allergies": ["SESAME", "PEANUTS"],
        "meals_per_day": 4,
        "hydration_liters_per_day": "2.5",
        **overrides,
    }
    return ProfileSectionCommand(parse_nutrition_habits(body), key or uuid4(), uuid4())


class NutritionHabitsPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._context = write_database("users_management_nutrition")
        self.db: WriteDatabase = self._context.__enter__()
        self.addCleanup(self._context.__exit__, None, None, None)

    def rows(self) -> list[NutritionHabitsRow]:
        with self.db.sessions() as session:
            return list(session.scalars(select(NutritionHabitsRow)))

    def habit_events(self) -> list[OutboxEventRow]:
        with self.db.sessions() as session:
            return list(
                session.scalars(
                    select(OutboxEventRow)
                    .where(OutboxEventRow.aggregate_type == "nutrition_habits")
                    .order_by(OutboxEventRow.aggregate_version)
                )
            )

    def test_create_and_replace_with_version(self) -> None:
        """CA1 y CA3: alta y reemplazo con versión, estado normalizado y un evento por cambio."""
        user_id = self.db.athlete()
        update_habits = self.db.update_nutrition_habits()

        first = update_habits(user_id, habits())
        second = update_habits(
            user_id,
            habits(expected_version=1, allergies=[], hydration_liters_per_day=None),
        )

        self.assertEqual((first.status, second.status), (201, 200))
        [row] = self.rows()
        self.assertEqual((row.version, row.diet_pattern, row.meals_per_day), (2, "VEGETARIAN", 4))
        self.assertEqual((row.restrictions, row.allergies), (["GLUTEN_FREE", "LOW_SUGAR"], []))
        self.assertIsNone(row.hydration_liters_per_day)
        self.assertEqual(row.health_consent_version, HEALTH_CONSENT)
        self.assertEqual(second.document["created_at"], first.document["created_at"])
        events = self.habit_events()
        self.assertEqual([e.aggregate_version for e in events], [1, 2])
        self.assertEqual({e.event_type for e in events}, {"habitos.alimenticios_actualizados.v1"})
        self.assertEqual(events[0].payload["hydration_liters_per_day"], "2.5")
        self.assertEqual(events[-1].payload, second.document)
        self.assertTrue(all(e.aggregate_id == user_id for e in events))

    def test_hydration_round_trips_with_one_decimal(self) -> None:
        user_id = self.db.athlete()

        created = self.db.update_nutrition_habits()(user_id, habits(hydration_liters_per_day="10"))

        [row] = self.rows()
        self.assertEqual(row.hydration_liters_per_day, Decimal("10.0"))
        self.assertEqual(created.document["hydration_liters_per_day"], "10.0")

    def test_rejections_leave_no_section_event_or_key(self) -> None:
        """CA2: `422` todo o nada (también sin consentimiento) y conflicto sin datos parciales."""
        user_id = self.db.athlete()
        update_habits = self.db.update_nutrition_habits()

        with self.assertRaises(InvalidProfileSection):
            update_habits(user_id, habits(allergies=["PEANUTS", "PEANUTS"]))
        with self.assertRaises(InvalidProfileSection):
            update_habits(user_id, habits(consent={"version": "otra"}))
        with self.assertRaises(VersionConflict):
            update_habits(user_id, habits(expected_version=1))

        self.assertEqual(self.rows(), [])
        self.assertEqual(self.habit_events(), [])
        with self.db.sessions() as session:
            keys = session.scalars(
                select(IdempotencyKeyRow.operation).where(
                    IdempotencyKeyRow.operation == "put_nutrition_habits"
                )
            ).all()
        self.assertEqual(keys, [])

    def test_eight_concurrent_replacements_create_one_version(self) -> None:
        """Ocho reemplazos con la misma `expected_version` → una versión y siete `409`."""
        user_id = self.db.athlete()
        update_habits = self.db.update_nutrition_habits()
        update_habits(user_id, habits())

        outcomes = run_concurrently(
            [
                lambda index=index: update_habits(
                    user_id, habits(expected_version=1, meals_per_day=1 + index % 8)
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
            (row.version, row.meals_per_day), (2, succeeded[0].document["meals_per_day"])
        )
        self.assertEqual([e.aggregate_version for e in self.habit_events()], [1, 2])

    def test_concurrent_retries_with_same_key_produce_one_effect(self) -> None:
        """CA6: reintentos simultáneos con la misma clave → mismo resultado y un evento."""
        user_id = self.db.athlete()
        update_habits = self.db.update_nutrition_habits()
        key = uuid4()

        outcomes = run_concurrently(
            [lambda: update_habits(user_id, habits(key)) for _ in range(CONCURRENT_REQUESTS)]
        )

        self.assertTrue(all(isinstance(item, ProfileSectionResult) for item in outcomes), outcomes)
        self.assertEqual(sum(not item.replayed for item in outcomes), 1)
        self.assertEqual(len(self.habit_events()), 1)
        with self.db.sessions() as session:
            [record] = session.scalars(
                select(IdempotencyKeyRow).where(IdempotencyKeyRow.key == key)
            )
        self.assertEqual((record.operation, record.resource_id), ("put_nutrition_habits", user_id))

    def test_database_rejects_out_of_contract_shapes(self) -> None:
        user_id = self.db.athlete()
        now = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
        valid: dict[str, Any] = {
            "user_id": user_id,
            "version": 1,
            "health_consent_version": HEALTH_CONSENT,
            "health_consent_at": now,
            "diet_pattern": "VEGAN",
            "restrictions": [],
            "allergies": [],
            "meals_per_day": 3,
            "hydration_liters_per_day": None,
            "created_at": now,
            "updated_at": now,
        }
        for overrides in (
            {"version": 0},
            {"diet_pattern": "PALEO"},
            {"restrictions": {"keto": True}},
            {"restrictions": ["KETO"] * 8},
            {"allergies": ["MILK"] * 10},
            {"meals_per_day": 0},
            {"meals_per_day": 9},
            {"hydration_liters_per_day": Decimal("0.4")},
            {"hydration_liters_per_day": Decimal("10.5")},
            {"user_id": uuid4()},  # FK local a `user_account`
        ):
            with self.subTest(overrides=list(overrides)), self.db.engine.connect() as connection:
                with self.assertRaises(IntegrityError):
                    connection.execute(insert(NutritionHabitsRow).values(**{**valid, **overrides}))


@contextmanager
def read_database_at(url: str) -> Iterator[ReadDatabase]:
    """Base de lectura ya creada en `url` (para probar una migración intermedia)."""
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        yield ReadDatabase(url, engine, sessionmaker(engine, expire_on_commit=False))
    finally:
        engine.dispose()


class NutritionHabitsMigrationTest(unittest.TestCase):
    def test_write_migration_is_reversible(self) -> None:
        """`0006` quita solo su tabla y se puede volver a aplicar."""
        with temporary_database("users_management_0006") as url:
            upgrade(WRITE_HISTORY, url)

            downgrade(WRITE_HISTORY, url, "0005_sport_habits")

            engine = create_engine(url, poolclass=pool.NullPool)
            try:
                tables = set(inspect(engine).get_table_names())
            finally:
                engine.dispose()
            self.assertEqual(current_revision(url), "0005_sport_habits")
            self.assertNotIn("nutrition_habits", tables)
            self.assertIn("sport_habits", tables)

            upgrade(WRITE_HISTORY, url)

            self.assertEqual(current_revision(url), "0006_nutrition_habits")

    def test_read_migration_is_reversible_and_keeps_existing_rows(self) -> None:
        """`0005` de lectura solo agrega columnas: las filas existentes quedan intactas."""
        with temporary_database("users_management_read_0005") as url:
            upgrade(READ_HISTORY, url, "0004_user_directory_full_name")
            with read_database_at(url) as read:
                read.projector()(
                    parse_users_event(profile_envelope(ProfileSection.DEMOGRAPHIC, uuid4(), 1))
                )

            upgrade(READ_HISTORY, url)
            with read_database_at(url) as read:
                [row] = read.profile_rows()
                self.assertEqual(
                    (row["nutrition_habits"], row["nutrition_habits_version"]), (None, None)
                )
                self.assertEqual(row["versions"][0], 1)

            downgrade(READ_HISTORY, url, "0004_user_directory_full_name")
            engine = create_engine(url, poolclass=pool.NullPool)
            try:
                columns = {c["name"] for c in inspect(engine).get_columns("athlete_profile_view")}
            finally:
                engine.dispose()
            self.assertNotIn("nutrition_habits", columns)
            self.assertIn("sport_habits", columns)


class NutritionHabitsProjectionTest(unittest.TestCase):
    """CQRS con la base de lectura real: duplicados, versión menor, fila ausente y
    reconstrucción; el sobre de la outbox llega a la vista y al contrato interno."""

    def setUp(self) -> None:
        self._read = read_database("users_management_nutrition_read")
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
        self.assertEqual((row["nutrition_habits"], row["nutrition_habits_version"]), (expected, 3))
        self.assertEqual(row["versions"], (None, None, None))

    def test_rebuild_reaches_the_same_view(self) -> None:
        user_id = uuid4()
        events = [profile_envelope(HABITS, user_id, version) for version in (1, 2, 3)]
        for event in events:
            self.apply(event)
        expected = self.read.profile_rows()

        with read_database("users_management_nutrition_rebuild") as rebuilt:
            project = rebuilt.projector()
            for event in reversed(events):
                project(parse_users_event(json.loads(json.dumps(event))))
            self.assertEqual(rebuilt.profile_rows(), expected)

    def test_command_event_reaches_the_profile_and_the_internal_contract(self) -> None:
        """CA1 y CA3: el sobre que publicaría la outbox se proyecta; el perfil lo muestra y el
        contrato interno devuelve solo patrón, restricciones, alergias y versión."""
        with write_database("users_management_nutrition_flow") as db:
            user_id = db.athlete()
            created = db.update_nutrition_habits()(user_id, habits())
            with db.sessions() as session:
                [event] = session.scalars(
                    select(OutboxEventRow).where(
                        OutboxEventRow.aggregate_type == "nutrition_habits"
                    )
                )
        self.apply(registered_envelope(user_id))
        before = self.read.restrictions().find(user_id)

        self.assertEqual(self.apply(_envelope_of(event)), ProjectionOutcome.APPLIED)

        view = self.read.reader().get(user_id)
        assert view is not None
        self.assertEqual(view.nutrition_habits_version, 1)
        self.assertEqual(
            view.nutrition_habits, {k: v for k, v in created.document.items() if k != "user_id"}
        )
        self.assertEqual(before, NO_HABITS)
        restrictions = self.read.restrictions().find(user_id)
        assert restrictions is not None
        self.assertEqual(
            (
                restrictions.diet_pattern,
                restrictions.restrictions,
                restrictions.allergies,
                restrictions.version,
            ),
            ("VEGETARIAN", ("GLUTEN_FREE", "LOW_SUGAR"), ("PEANUTS", "SESAME"), 1),
        )

    def test_internal_contract_requires_an_active_account(self) -> None:
        """Cuenta inexistente o deshabilitada → no se resuelve, aunque la vista tenga hábitos."""
        user_id = uuid4()
        self.apply(registered_envelope(user_id))
        self.apply(profile_envelope(HABITS, user_id, 1))
        with self.read.sessions() as session:
            session.execute(
                update(UserDirectoryRow)
                .where(UserDirectoryRow.user_id == user_id)
                .values(status="disabled")
            )
            session.commit()

        self.assertIsNone(self.read.restrictions().find(user_id))
        self.assertIsNone(self.read.restrictions().find(uuid4()))


if __name__ == "__main__":
    unittest.main()
