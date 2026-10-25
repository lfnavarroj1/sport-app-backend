"""HU038 contra PostgreSQL real: preferencias de recordatorios con `expected_version`, creación
concurrente (una sola fila, el resto `version_conflict`), PUT idempotente y restricciones de la
tabla `session_reminder_preference`."""

import threading
import unittest
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from support import TrainingDatabases, athlete_and_professional, training_databases

from training_management.application.commands.session_reminder_preferences import (
    UpdateSessionReminderPreferences,
)
from training_management.application.queries.session_reminders import (
    GetSessionReminderPreferences,
)
from training_management.domain.reminders.model import (
    ReminderPreferencesVersionConflict,
    ReminderSetting,
)
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.persistence_write.models import (
    SessionReminderPreferenceRow,
)
from training_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemySessionReminderPreferencesUnitOfWork,
)


class AlwaysCapable:
    def has_capability(self, user_id: Any, capability: str) -> bool:
        return True


def body(hydration: int, nutrition: int, expected_version: int) -> dict[str, Any]:
    return {
        "hydration": {"enabled": True, "interval_minutes": hydration},
        "nutrition": {"enabled": False, "interval_minutes": nutrition},
        "expected_version": expected_version,
    }


class SessionReminderPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: TrainingDatabases = self.enterContext(training_databases("hu038"))
        self.athlete, _ = athlete_and_professional()

        def unit_of_work() -> SqlAlchemySessionReminderPreferencesUnitOfWork:
            return SqlAlchemySessionReminderPreferencesUnitOfWork(self.db.writes)

        self.update = UpdateSessionReminderPreferences(
            unit_of_work=unit_of_work, capabilities=AlwaysCapable(), clock=SystemClock()
        )
        self.read = GetSessionReminderPreferences(
            unit_of_work=unit_of_work, capabilities=AlwaysCapable()
        )

    def rows(self) -> list[SessionReminderPreferenceRow]:
        with self.db.writes() as session:
            return list(session.scalars(select(SessionReminderPreferenceRow)))

    def test_default_then_versions_advance_and_read_back(self) -> None:
        self.assertEqual(self.read(self.athlete).version, 0)
        first = self.update(self.athlete, body(10, 120, 0))
        same = self.update(self.athlete, body(10, 120, 1))
        second = self.update(self.athlete, body(60, 30, 1))
        self.assertEqual((first.version, same.version, second.version), (1, 1, 2))
        stored = self.read(self.athlete)
        self.assertEqual(
            (stored.hydration, stored.nutrition, stored.version),
            (ReminderSetting(True, 60), ReminderSetting(False, 30), 2),
        )
        (row,) = self.rows()
        self.assertEqual(row.created_at, first.created_at)
        self.assertGreaterEqual(row.updated_at, row.created_at)
        with self.assertRaises(ReminderPreferencesVersionConflict) as caught:
            self.update(self.athlete, body(20, 45, 1))
        self.assertEqual(caught.exception.current_version, 2)
        self.assertEqual(self.read(self.athlete).hydration.interval_minutes, 60)

    def test_concurrent_first_saves_create_one_row_and_the_rest_conflict(self) -> None:
        count = 5
        barrier = threading.Barrier(count)
        results: list[Any] = []
        errors: list[Exception] = []
        values = iter(range(20, 20 + count))
        lock = threading.Lock()

        def run() -> None:
            with lock:
                hydration = next(values)
            barrier.wait()
            try:
                results.append(self.update(self.athlete, body(hydration, 45, 0)))
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=run) for _ in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), count - 1)
        self.assertTrue(all(isinstance(e, ReminderPreferencesVersionConflict) for e in errors))
        (row,) = self.rows()
        self.assertEqual(
            (row.version, row.hydration_interval_minutes),
            (1, results[0].hydration.interval_minutes),
        )

    def test_database_rejects_out_of_range_rows(self) -> None:
        statements = [
            "INSERT INTO session_reminder_preference VALUES "
            "(gen_random_uuid(), true, 9, true, 45, 1, now(), now())",
            "INSERT INTO session_reminder_preference VALUES "
            "(gen_random_uuid(), true, 15, true, 121, 1, now(), now())",
            "INSERT INTO session_reminder_preference VALUES "
            "(gen_random_uuid(), true, 15, true, 45, 0, now(), now())",
        ]
        for statement in statements:
            with self.subTest(statement=statement), self.assertRaises(IntegrityError):
                with self.db.writes() as session, session.begin():
                    session.execute(text(statement))


if __name__ == "__main__":
    unittest.main()
