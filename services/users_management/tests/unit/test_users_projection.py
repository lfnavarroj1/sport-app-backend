"""Reglas de `usuarios-read-projector` sin infraestructura (CMP-001, ADR-034)."""

import unittest
from dataclasses import replace
from uuid import UUID, uuid4

from projection_fakes import (
    FixedClock,
    InMemoryProjectionUnitOfWork,
    InMemoryReadStore,
    profile_envelope,
    registered_envelope,
)

from users_management.application.messages import (
    ProfileSectionUpdatedEvent,
    UserRegisteredEvent,
    parse_users_event,
)
from users_management.application.projections.users_projection import (
    DIRECTORY_PROJECTION,
    PROFILE_PROJECTION,
    ProjectionGap,
    ProjectionOutcome,
    UsersProjector,
)
from users_management.domain.profile.events import ProfileSection


def section_event(
    section: ProfileSection, user_id: UUID | None = None, version: int = 1
) -> ProfileSectionUpdatedEvent:
    event = parse_users_event(profile_envelope(section, user_id or uuid4(), version))
    assert isinstance(event, ProfileSectionUpdatedEvent)
    return event


def registration_event() -> UserRegisteredEvent:
    event = parse_users_event(registered_envelope())
    assert isinstance(event, UserRegisteredEvent)
    return event


class UsersProjectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryReadStore()
        self.project = UsersProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.store), clock=FixedClock()
        )

    def test_registration_fills_the_directory_once(self) -> None:
        event = registration_event()

        first = self.project(event)
        again = self.project(event)

        self.assertEqual((first, again), (ProjectionOutcome.APPLIED, ProjectionOutcome.DUPLICATE))
        entry = self.store.directory[event.user_id]
        self.assertEqual(
            (entry.email, entry.actor_type, entry.status), (event.email, "athlete", "active")
        )
        self.assertEqual(self.store.processed[event.event_id][0], DIRECTORY_PROJECTION)

    def test_registration_redelivered_with_another_event_id_is_obsolete(self) -> None:
        event = registration_event()
        self.project(event)

        outcome = self.project(replace(event, event_id=uuid4()))

        self.assertEqual(outcome, ProjectionOutcome.OBSOLETE)
        self.assertEqual(len(self.store.processed), 2, "el obsoleto también queda registrado")

    def test_directory_gap_fails_without_effects(self) -> None:
        """Un hueco de versión no se adivina: falla y no registra el evento (CMP-001)."""
        event = replace(registration_event(), aggregate_version=3)

        with self.assertRaises(ProjectionGap):
            self.project(event)

        self.assertEqual(self.store.directory, {})
        self.assertEqual(self.store.processed, {})

    def test_section_creates_the_row_and_keeps_other_sections(self) -> None:
        """CA7: cada sección se proyecta una vez y sin tocar las demás."""
        user_id = uuid4()
        demographic = section_event(ProfileSection.DEMOGRAPHIC, user_id, 1)
        physiological = section_event(ProfileSection.PHYSIOLOGICAL, user_id, 1)

        outcomes = [self.project(demographic), self.project(physiological)]

        self.assertEqual(outcomes, [ProjectionOutcome.APPLIED] * 2)
        row = self.store.profiles[user_id]
        self.assertEqual(row["demographic"], demographic.document)
        self.assertEqual(row["physiological"], physiological.document)
        self.assertEqual((row["demographic_version"], row["physiological_version"]), (1, 1))
        self.assertNotIn("user_id", row["demographic"])
        self.assertEqual(self.store.processed[demographic.event_id][0], PROFILE_PROJECTION)

    def test_higher_version_replaces_and_lower_is_obsolete(self) -> None:
        """Estado completo por sección: aplica la versión mayor aunque falten intermedias."""
        user_id = uuid4()
        self.project(section_event(ProfileSection.DEMOGRAPHIC, user_id, 1))
        newest = section_event(ProfileSection.DEMOGRAPHIC, user_id, 4)

        applied = self.project(newest)
        late = self.project(section_event(ProfileSection.DEMOGRAPHIC, user_id, 3))
        same = self.project(section_event(ProfileSection.DEMOGRAPHIC, user_id, 4))

        self.assertEqual(applied, ProjectionOutcome.APPLIED)
        self.assertEqual((late, same), (ProjectionOutcome.OBSOLETE, ProjectionOutcome.OBSOLETE))
        self.assertEqual(self.store.profiles[user_id]["demographic_version"], 4)
        self.assertEqual(self.store.profiles[user_id]["demographic"], newest.document)

    def test_duplicate_section_event_is_not_reapplied(self) -> None:
        event = section_event(ProfileSection.PHYSIOLOGICAL)
        self.project(event)
        commits = self.store.commits

        outcome = self.project(event)

        self.assertEqual(outcome, ProjectionOutcome.DUPLICATE)
        self.assertEqual(self.store.commits, commits, "un duplicado no confirma cambios")


if __name__ == "__main__":
    unittest.main()
