"""`usuarios-read-projector` contra PostgreSQL real (requiere `TEST_DATABASE_URL`).

Duplicados concurrentes, versiones atrasadas, secciones independientes, hueco de versión
en el directorio, reconstrucción desde los eventos y lectura del perfil (CMP-001, ADR-034).
"""

import threading
import unittest
from dataclasses import replace
from typing import Any
from uuid import uuid4

from projection_support import (
    profile_envelope,
    read_database,
    registered_envelope,
)
from sqlalchemy import create_engine, pool
from sqlalchemy.orm import sessionmaker

from users_management.application.errors import ReadModelUnavailable
from users_management.application.messages import (
    UserRegisteredEvent,
    UsersEvent,
    parse_users_event,
)
from users_management.application.projections.users_projection import (
    ProjectionGap,
    ProjectionOutcome,
)
from users_management.domain.profile.events import ProfileSection
from users_management.infrastructure.persistence_read.models import (
    AthleteProfileViewRow,
    ProcessedEventRow,
    UserDirectoryRow,
)
from users_management.infrastructure.persistence_read.profile import (
    SqlAlchemyAthleteProfileReader,
)

DEMOGRAPHIC, PHYSIOLOGICAL = ProfileSection.DEMOGRAPHIC, ProfileSection.PHYSIOLOGICAL


def parsed(body: dict[str, Any]) -> UsersEvent:
    return parse_users_event(body)


class ReadProjectionIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = self.enterContext(read_database("users_projection"))
        self.project = self.db.projector()

    def run_concurrently(self, events: list[UsersEvent]) -> list[ProjectionOutcome]:
        barrier = threading.Barrier(len(events))
        outcomes: list[ProjectionOutcome] = []
        errors: list[BaseException] = []

        def worker(event: UsersEvent) -> None:
            barrier.wait()
            try:
                outcomes.append(self.project(event))
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(event,)) for event in events]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        return outcomes

    def test_concurrent_deliveries_of_the_same_event_apply_once(self) -> None:
        """CA7 / CMP-001 prueba 13: entregas simultáneas del mismo `event_id`."""
        event = parsed(profile_envelope(PHYSIOLOGICAL, uuid4(), 1))

        outcomes = self.run_concurrently([event] * 6)

        self.assertEqual(outcomes.count(ProjectionOutcome.APPLIED), 1)
        self.assertEqual(outcomes.count(ProjectionOutcome.DUPLICATE), 5)
        self.assertEqual(self.db.count(ProcessedEventRow), 1)
        self.assertEqual(self.db.count(AthleteProfileViewRow), 1)

    def test_absent_row_is_inserted_and_sections_are_independent(self) -> None:
        """CA7: cada sección se aplica sin tocar la otra, aunque lleguen a la vez."""
        user_id = uuid4()
        demographic = parsed(profile_envelope(DEMOGRAPHIC, user_id, 2))
        physiological = parsed(profile_envelope(PHYSIOLOGICAL, user_id, 1))

        outcomes = self.run_concurrently([demographic, physiological])

        self.assertEqual(outcomes, [ProjectionOutcome.APPLIED] * 2)
        view = self.db.reader().get(user_id)
        assert view is not None
        self.assertEqual(view.demographic, demographic.document)
        self.assertEqual(view.physiological, physiological.document)
        self.assertEqual((view.demographic_version, view.physiological_version), (2, 1))
        self.assertIsNone(view.sport_habits)
        self.assertIsNone(view.sport_habits_version)
        self.assertIsNotNone(view.projected_at)

    def test_lower_or_equal_version_is_ignored(self) -> None:
        user_id = uuid4()
        newest = parsed(profile_envelope(DEMOGRAPHIC, user_id, 5))
        self.assertEqual(self.project(newest), ProjectionOutcome.APPLIED)

        late = self.project(parsed(profile_envelope(DEMOGRAPHIC, user_id, 4)))
        same = self.project(parsed(profile_envelope(DEMOGRAPHIC, user_id, 5)))

        self.assertEqual((late, same), (ProjectionOutcome.OBSOLETE, ProjectionOutcome.OBSOLETE))
        view = self.db.reader().get(user_id)
        assert view is not None
        self.assertEqual(view.demographic, newest.document)
        self.assertEqual(view.demographic_version, 5)
        self.assertEqual(self.db.count(ProcessedEventRow), 3, "los obsoletos quedan registrados")

    def test_registration_fills_the_directory_and_gap_does_not_corrupt_it(self) -> None:
        """Directorio: solo la versión siguiente; un hueco revierte sin tocar la fila."""
        registration = parsed(registered_envelope())
        assert isinstance(registration, UserRegisteredEvent)
        self.assertEqual(self.project(registration), ProjectionOutcome.APPLIED)
        gap = replace(registration, event_id=uuid4(), aggregate_version=3)
        other_user_gap = replace(
            registration, event_id=uuid4(), user_id=uuid4(), aggregate_version=2
        )

        for event in (gap, other_user_gap):
            with self.subTest(event=event), self.assertRaises(ProjectionGap):
                self.project(event)
        redelivered = self.project(replace(registration, event_id=uuid4()))

        row = self.db.directory(registration.user_id)
        assert row is not None
        self.assertEqual(redelivered, ProjectionOutcome.OBSOLETE)
        self.assertEqual(
            (row.email, row.actor_type, row.status, row.aggregate_version),
            (registration.email, "athlete", "active", 1),
        )
        self.assertEqual(self.db.count(UserDirectoryRow), 1)
        self.assertEqual(self.db.count(ProcessedEventRow), 2, "el hueco no queda registrado")

    def test_concurrent_registrations_of_the_same_user_insert_once(self) -> None:
        registration = parsed(registered_envelope())
        copies = [replace(registration, event_id=uuid4()) for _ in range(4)]

        outcomes = self.run_concurrently(copies)

        self.assertEqual(outcomes.count(ProjectionOutcome.APPLIED), 1)
        self.assertEqual(outcomes.count(ProjectionOutcome.OBSOLETE), 3)
        self.assertEqual(self.db.count(UserDirectoryRow), 1)

    def test_rebuilding_from_the_events_gives_the_same_view(self) -> None:
        """CMP-001 prueba 7: reaplicar los eventos en una base vacía reconstruye la vista,
        aunque lleguen desordenados y con duplicados."""
        users = [uuid4() for _ in range(3)]
        events = [registered_envelope(user_id) for user_id in users]
        for user_id in users:
            for version in (1, 2, 3):
                events.append(profile_envelope(DEMOGRAPHIC, user_id, version))
            for version in (1, 2):
                events.append(profile_envelope(PHYSIOLOGICAL, user_id, version))
        for body in events:
            self.project(parsed(body))
        expected = self.db.profile_rows()

        # Primero las versiones más altas y después, de nuevo, una de cada tres entregas.
        replay = list(reversed(events)) + events[::3]
        with read_database("users_projection_rebuild") as rebuilt:
            projector = rebuilt.projector()
            for body in replay:
                projector(parsed(body))

            self.assertEqual(rebuilt.profile_rows(), expected)
            self.assertEqual(rebuilt.count(UserDirectoryRow), len(users))
            self.assertEqual(rebuilt.count(ProcessedEventRow), len(events))
        self.assertEqual([row["versions"] for row in expected], [(3, 2, None)] * len(users))


class AthleteProfileReaderIntegrationTest(unittest.TestCase):
    def test_get_with_and_without_row(self) -> None:
        with read_database("users_profile_reader") as db:
            user_id = uuid4()
            event = parsed(profile_envelope(DEMOGRAPHIC, user_id, 1))
            db.projector()(event)

            view = db.reader().get(user_id)
            missing = db.reader().get(uuid4())

        assert view is not None
        self.assertEqual(view.user_id, user_id)
        self.assertEqual(view.demographic, event.document)
        self.assertIsNone(view.physiological)
        self.assertIsNone(view.physiological_version)
        self.assertIsNone(missing)
        self.assertNotIn("Ciudad", repr(view))

    def test_unreachable_read_database_raises_read_model_unavailable(self) -> None:
        engine = create_engine(
            "postgresql+psycopg://nadie@127.0.0.1:1/ninguna",
            poolclass=pool.NullPool,
            connect_args={"connect_timeout": 2},
        )
        self.addCleanup(engine.dispose)
        reader = SqlAlchemyAthleteProfileReader(sessionmaker(engine))

        with self.assertRaises(ReadModelUnavailable) as raised:
            reader.get(uuid4())

        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(raised.exception.code, "upstream_unavailable")


if __name__ == "__main__":
    unittest.main()
