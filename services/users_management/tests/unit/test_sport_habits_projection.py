"""HU006 en `usuarios-read-projector` sin infraestructura (CMP-001, ADR-034): lectura
validada de `habitos.deportivos_actualizados.v1` y proyección de estado completo en
`athlete_profile_view.sport_habits`."""

import unittest
from typing import Any
from uuid import uuid4

from projection_fakes import (
    FixedClock,
    InMemoryProjectionUnitOfWork,
    InMemoryReadStore,
    profile_envelope,
    sport_habits_payload,
)
from test_users_projection import section_event

from users_management.application.messages import (
    InvalidEvent,
    ProfileSectionUpdatedEvent,
    parse_users_event,
)
from users_management.application.projections.users_projection import (
    ProjectionOutcome,
    UsersProjector,
)
from users_management.domain.profile.events import ProfileSection

HABITS = ProfileSection.SPORT_HABITS


def habits_envelope(**payload_overrides: Any) -> dict[str, Any]:
    user_id = uuid4()
    return profile_envelope(
        HABITS, user_id, 1, payload=sport_habits_payload(user_id, 1, **payload_overrides)
    )


class SportHabitsEventParsingTest(unittest.TestCase):
    def test_valid_event_is_read_as_the_sport_habits_section(self) -> None:
        event = parse_users_event(habits_envelope())

        assert isinstance(event, ProfileSectionUpdatedEvent)
        self.assertEqual(
            (event.section, event.event_type),
            (HABITS, "habitos.deportivos_actualizados.v1"),
        )
        self.assertNotIn("user_id", event.document)
        self.assertEqual(event.document["availability"]["days"], ["MON", "WED", "SAT"])

    def test_events_outside_the_contract_are_invalid(self) -> None:
        running = sport_habits_payload(uuid4())["sports"][0]
        cases: dict[str, dict[str, Any]] = {
            "campo de más": {"weight_kg": "68.5"},
            "sin deportes": {"sports": []},
            "seis deportes": {"sports": [running] * 6},
            "deporte repetido": {"sports": [running, running]},
            "deporte desconocido": {"sports": [{**running, "sport": "tennis"}]},
            "frecuencia fuera de rango": {"sports": [{**running, "frequency_per_week": 8}]},
            "orden no canónico de deportes": {
                "sports": [{**running, "sport": "swimming"}, running]
            },
            "días sin orden canónico": {
                "availability": {"days": ["SAT", "MON"], "time_slots": ["NIGHT"]}
            },
            "franja repetida": {
                "availability": {"days": ["MON"], "time_slots": ["NIGHT", "NIGHT"]}
            },
            "objetivo desconocido": {"primary_goal": "FUN"},
        }
        for case, overrides in cases.items():
            with self.subTest(case=case), self.assertRaises(InvalidEvent):
                parse_users_event(habits_envelope(**overrides))


class SportHabitsProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryReadStore()
        self.project = UsersProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.store), clock=FixedClock()
        )

    def test_missing_row_is_inserted_and_other_sections_are_kept(self) -> None:
        user_id = uuid4()
        demographic = section_event(ProfileSection.DEMOGRAPHIC, user_id, 2)
        habits = section_event(HABITS, user_id, 1)

        outcomes = [self.project(habits), self.project(demographic)]

        self.assertEqual(outcomes, [ProjectionOutcome.APPLIED] * 2)
        row = self.store.profiles[user_id]
        self.assertEqual((row["sport_habits"], row["sport_habits_version"]), (habits.document, 1))
        self.assertEqual(row["demographic_version"], 2)

    def test_duplicates_and_lower_versions_are_ignored(self) -> None:
        """CQRS: un duplicado no confirma nada; una versión menor o igual es obsoleta."""
        user_id = uuid4()
        newest = section_event(HABITS, user_id, 3)
        self.project(newest)
        commits = self.store.commits

        duplicate = self.project(newest)
        late = self.project(section_event(HABITS, user_id, 2))

        self.assertEqual(
            (duplicate, late), (ProjectionOutcome.DUPLICATE, ProjectionOutcome.OBSOLETE)
        )
        self.assertEqual(self.store.profiles[user_id]["sport_habits"], newest.document)
        self.assertEqual(self.store.commits, commits + 1, "solo el obsoleto registra su event_id")

    def test_rebuild_from_the_events_reaches_the_same_view(self) -> None:
        user_id = uuid4()
        events = [section_event(HABITS, user_id, version) for version in (1, 2, 3)]
        for event in events:
            self.project(event)
        expected = dict(self.store.profiles[user_id])

        rebuilt = InMemoryReadStore()
        project = UsersProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(rebuilt), clock=FixedClock()
        )
        for event in reversed(events):  # sin orden global garantizado
            project(event)

        self.assertEqual(rebuilt.profiles[user_id], expected)


if __name__ == "__main__":
    unittest.main()
