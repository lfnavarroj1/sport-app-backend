"""HU009 en `usuarios-read-projector` sin infraestructura (CMP-001, ADR-035): lectura validada
de `habitos.alimenticios_actualizados.v1` y proyección de estado completo en
`athlete_profile_view.nutrition_habits`."""

import unittest
from typing import Any
from uuid import uuid4

from projection_fakes import (
    FixedClock,
    InMemoryProjectionUnitOfWork,
    InMemoryReadStore,
    nutrition_habits_payload,
    profile_envelope,
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

HABITS = ProfileSection.NUTRITION_HABITS


def habits_envelope(**payload_overrides: Any) -> dict[str, Any]:
    user_id = uuid4()
    return profile_envelope(
        HABITS, user_id, 1, payload=nutrition_habits_payload(user_id, 1, **payload_overrides)
    )


class NutritionHabitsEventParsingTest(unittest.TestCase):
    def test_valid_event_is_read_as_the_nutrition_habits_section(self) -> None:
        event = parse_users_event(habits_envelope())

        assert isinstance(event, ProfileSectionUpdatedEvent)
        self.assertEqual(
            (event.section, event.event_type),
            (HABITS, "habitos.alimenticios_actualizados.v1"),
        )
        self.assertNotIn("user_id", event.document)
        self.assertEqual(event.document["allergies"], ["PEANUTS", "SESAME"])

    def test_empty_lists_and_missing_hydration_are_valid(self) -> None:
        event = parse_users_event(
            habits_envelope(restrictions=[], allergies=[], hydration_liters_per_day=None)
        )

        assert isinstance(event, ProfileSectionUpdatedEvent)
        self.assertEqual((event.document["restrictions"], event.document["allergies"]), ([], []))

    def test_events_outside_the_contract_are_invalid(self) -> None:
        cases: dict[str, dict[str, Any]] = {
            "campo de más": {"sports": []},
            "texto libre": {"notes": "kiwi"},
            "patrón desconocido": {"diet_pattern": "PALEO"},
            "restricción desconocida": {"restrictions": ["SIN_AZUCAR"]},
            "alergia repetida": {"allergies": ["MILK", "MILK"]},
            "orden no canónico": {"allergies": ["SESAME", "PEANUTS"]},
            "lista que no es lista": {"restrictions": "KETO"},
            "comidas fuera de rango": {"meals_per_day": 9},
            "hidratación con dos decimales": {"hydration_liters_per_day": "2.55"},
            "hidratación fuera de rango": {"hydration_liters_per_day": "10.5"},
            "hidratación como número": {"hydration_liters_per_day": 2.5},
            "sin consentimiento": {"consent": None},
            "consentimiento incompleto": {"consent": {"version": "health-test.v1"}},
        }
        for case, overrides in cases.items():
            with self.subTest(case=case), self.assertRaises(InvalidEvent):
                parse_users_event(habits_envelope(**overrides))

    def test_rejection_reasons_never_carry_values(self) -> None:
        with self.assertRaises(InvalidEvent) as raised:
            parse_users_event(habits_envelope(allergies=["PEANUTS", "KIWI"]))

        self.assertEqual(raised.exception.reason, "allergies_invalid")
        self.assertNotIn("KIWI", str(raised.exception))


class NutritionHabitsProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryReadStore()
        self.project = UsersProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.store), clock=FixedClock()
        )

    def test_missing_row_is_inserted_and_other_sections_are_kept(self) -> None:
        """CA1: el perfil refleja la sección una vez proyectada, sin tocar las demás."""
        user_id = uuid4()
        sport = section_event(ProfileSection.SPORT_HABITS, user_id, 2)
        habits = section_event(HABITS, user_id, 1)

        outcomes = [self.project(habits), self.project(sport)]

        self.assertEqual(outcomes, [ProjectionOutcome.APPLIED] * 2)
        row = self.store.profiles[user_id]
        self.assertEqual(
            (row["nutrition_habits"], row["nutrition_habits_version"]), (habits.document, 1)
        )
        self.assertEqual(row["sport_habits_version"], 2)

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
        self.assertEqual(self.store.profiles[user_id]["nutrition_habits"], newest.document)
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
