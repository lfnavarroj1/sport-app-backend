"""HU026 (ADR-041) contra PostgreSQL real (requiere `TEST_DATABASE_URL`): el contrato interno
`athlete-context` lee las secciones `demographic` y `sport_habits` que proyecta
`usuarios-read-projector` en `athlete_profile_view`, sin ninguna otra sección, y solo para
cuentas activas."""

import json
import unittest
from typing import Any
from uuid import uuid4

from projection_support import ReadDatabase, profile_envelope, read_database, registered_envelope
from sqlalchemy import update

from users_management.application.messages import parse_users_event
from users_management.domain.profile.events import ProfileSection
from users_management.infrastructure.persistence_read.athlete_context import (
    SqlAlchemyAthleteContextReader,
)
from users_management.infrastructure.persistence_read.models import UserDirectoryRow


class AthleteContextPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.read: ReadDatabase = self.enterContext(read_database("users_management_context"))
        self.project = self.read.projector()
        self.reader = SqlAlchemyAthleteContextReader(self.read.sessions)

    def apply(self, envelope: dict[str, Any]) -> None:
        self.project(parse_users_event(json.loads(json.dumps(envelope))))

    def test_projected_sections_reach_the_context(self) -> None:
        """Deportes y objetivo de HU006; ciudad, país y nacimiento de HU005 (versión vigente)."""
        user_id = uuid4()
        self.apply(registered_envelope(user_id))
        for section in ProfileSection:
            self.apply(profile_envelope(section, user_id, 1))
        self.apply(profile_envelope(ProfileSection.DEMOGRAPHIC, user_id, 2))

        context = self.reader.find(user_id)

        assert context is not None
        self.assertEqual(
            (
                context.sports,
                context.primary_goal,
                context.city,
                context.country_code,
                context.birth_date,
            ),
            (("running",), None, "Ciudad 2", "CO", "1990-05-14"),
        )

    def test_account_without_profile_has_an_empty_context(self) -> None:
        user_id = uuid4()
        self.apply(registered_envelope(user_id))

        context = self.reader.find(user_id)

        assert context is not None
        self.assertTrue(context.is_empty)
        self.assertEqual((context.sports, context.city), ((), None))

    def test_only_sport_habits_leave_the_demographic_fields_empty(self) -> None:
        user_id = uuid4()
        self.apply(registered_envelope(user_id))
        self.apply(profile_envelope(ProfileSection.SPORT_HABITS, user_id, 1))

        context = self.reader.find(user_id)

        assert context is not None
        self.assertFalse(context.is_empty)
        self.assertEqual(
            (context.sports, context.city, context.birth_date), (("running",), None, None)
        )

    def test_unknown_or_disabled_account_is_not_resolved(self) -> None:
        user_id = uuid4()
        self.apply(registered_envelope(user_id))
        self.apply(profile_envelope(ProfileSection.SPORT_HABITS, user_id, 1))
        with self.read.sessions() as session:
            session.execute(
                update(UserDirectoryRow)
                .where(UserDirectoryRow.user_id == user_id)
                .values(status="disabled")
            )
            session.commit()

        self.assertIsNone(self.reader.find(user_id))
        self.assertIsNone(self.reader.find(uuid4()))


if __name__ == "__main__":
    unittest.main()
