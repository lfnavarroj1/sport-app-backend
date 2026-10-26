"""HU006 (ADR-034): reglas de la sección de hábitos deportivos sin infraestructura.

Se recorre el camino real del transporte (`parse_sport_habits`) y del dominio, así cada
caso prueba también la ruta del error (`sports[1].sport`, `availability.days[0]`).
"""

import unittest
from typing import Any

from fakes import sport_habits_body

from users_management.api.profile_payload import parse_sport_habits
from users_management.domain.profile.sport_habits import (
    SportHabitsSection,
    validate_sport_habits,
)
from users_management.domain.profile.validation import InvalidProfileSection

RUNNING: dict[str, Any] = {
    "sport": "running",
    "frequency_per_week": 3,
    "session_duration_minutes": 45,
    "intensity": "MODERATE",
    "level": "INTERMEDIATE",
}


def sport(**overrides: Any) -> dict[str, Any]:
    return {**RUNNING, **overrides}


def validate(body: Any) -> SportHabitsSection:
    return validate_sport_habits(parse_sport_habits(body))


def problems(body: Any) -> set[tuple[str | None, str]]:
    try:
        validate(body)
    except InvalidProfileSection as error:
        return {(item.field, item.issue.value) for item in error.violations}
    raise AssertionError("se esperaba validation_error")


class SportHabitsNormalizationTest(unittest.TestCase):
    def test_valid_body_is_normalized_in_catalog_order(self) -> None:
        """CA1: deportes, días y franjas en el orden de su catálogo, opcionales en `null`."""
        section = validate(sport_habits_body())

        self.assertEqual(
            section.fields(now=None),  # type: ignore[arg-type]
            {
                "sports": [
                    {
                        "sport": "running",
                        "frequency_per_week": 3,
                        "session_duration_minutes": 45,
                        "intensity": "MODERATE",
                        "level": "INTERMEDIATE",
                        "years_practicing": 4,
                    },
                    {
                        "sport": "swimming",
                        "frequency_per_week": 2,
                        "session_duration_minutes": 60,
                        "intensity": "LOW",
                        "level": "BEGINNER",
                        "years_practicing": None,
                    },
                ],
                "availability": {
                    "days": ["MON", "WED", "SAT"],
                    "time_slots": ["MORNING", "EVENING"],
                },
                "primary_goal": "HEALTH",
            },
        )
        self.assertIsNone(section.expected_version)

    def test_same_information_in_another_order_normalizes_equally(self) -> None:
        """La huella idempotente se calcula sobre listas en orden canónico."""
        reordered = sport_habits_body()
        reordered["sports"] = list(reversed(reordered["sports"]))
        reordered["availability"] = {
            "days": ["WED", "SAT", "MON"],
            "time_slots": ["MORNING", "EVENING"],
        }

        self.assertEqual(
            validate(reordered).normalized(), validate(sport_habits_body()).normalized()
        )

    def test_limits_are_inclusive(self) -> None:
        body = sport_habits_body(
            sports=[
                sport(
                    sport=name,
                    frequency_per_week=frequency,
                    session_duration_minutes=duration,
                    years_practicing=years,
                )
                for name, frequency, duration, years in (
                    ("running", 1, 10, 0),
                    ("cycling", 7, 600, 80),
                    ("swimming", 4, 90, None),
                    ("strength", 2, 30, 1),
                    ("other", 3, 20, 5),
                )
            ],
            availability={
                "days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
                "time_slots": ["MORNING", "AFTERNOON", "EVENING", "NIGHT"],
            },
            primary_goal=None,
        )

        section = validate(body)

        self.assertEqual(len(section.sports), 5)
        self.assertIsNone(section.primary_goal)
        self.assertEqual(len(section.days), 7)

    def test_every_catalog_value_is_accepted(self) -> None:
        for intensity in ("LOW", "MODERATE", "HIGH"):
            for level in ("BEGINNER", "INTERMEDIATE", "ADVANCED"):
                with self.subTest(intensity=intensity, level=level):
                    validate(sport_habits_body(sports=[sport(intensity=intensity, level=level)]))
        for goal in ("HEALTH", "PERFORMANCE", "WEIGHT_MANAGEMENT", "RECREATION", "COMPETITION"):
            with self.subTest(goal=goal):
                self.assertEqual(validate(sport_habits_body(primary_goal=goal)).primary_goal, goal)


class SportHabitsRulesTest(unittest.TestCase):
    """CA2: cada regla incumplida responde `validation_error` con su campo e `issue`."""

    def test_zero_and_six_sports(self) -> None:
        six = [
            sport(sport=name) for name in ("running", "cycling", "swimming", "strength", "other")
        ]
        six.append(sport(sport="running"))

        self.assertEqual(problems(sport_habits_body(sports=[])), {("sports", "required")})
        self.assertEqual(problems(sport_habits_body(sports=six)), {("sports", "too_many_items")})

    def test_repeated_or_unknown_sport(self) -> None:
        repeated = sport_habits_body(sports=[sport(), sport(sport="cycling"), sport()])
        unknown = sport_habits_body(sports=[sport(sport="tennis")])

        self.assertEqual(problems(repeated), {("sports[2].sport", "duplicate_value")})
        self.assertEqual(problems(unknown), {("sports[0].sport", "unknown_value")})

    def test_out_of_range_and_unknown_values_in_a_sport(self) -> None:
        body = sport_habits_body(
            sports=[
                sport(
                    frequency_per_week=8,
                    session_duration_minutes=9,
                    intensity="EXTREME",
                    level="PRO",
                    years_practicing=81,
                ),
                sport(sport="cycling", frequency_per_week=0, session_duration_minutes=601),
            ]
        )

        self.assertEqual(
            problems(body),
            {
                ("sports[0].frequency_per_week", "out_of_range"),
                ("sports[0].session_duration_minutes", "out_of_range"),
                ("sports[0].intensity", "unknown_value"),
                ("sports[0].level", "unknown_value"),
                ("sports[0].years_practicing", "out_of_range"),
                ("sports[1].frequency_per_week", "out_of_range"),
                ("sports[1].session_duration_minutes", "out_of_range"),
            },
        )

    def test_missing_fields_of_a_sport(self) -> None:
        self.assertEqual(
            problems(sport_habits_body(sports=[{"sport": "running"}])),
            {
                ("sports[0].frequency_per_week", "required"),
                ("sports[0].session_duration_minutes", "required"),
                ("sports[0].intensity", "required"),
                ("sports[0].level", "required"),
            },
        )

    def test_repeated_or_unknown_days_and_slots(self) -> None:
        body = sport_habits_body(
            availability={"days": ["MON", "MON", "FUN"], "time_slots": ["NIGHT", "NIGHT"]}
        )

        self.assertEqual(
            problems(body),
            {
                ("availability.days[1]", "duplicate_value"),
                ("availability.days[2]", "unknown_value"),
                ("availability.time_slots[1]", "duplicate_value"),
            },
        )

    def test_empty_or_too_long_availability_lists(self) -> None:
        empty = sport_habits_body(availability={"days": [], "time_slots": []})
        long = sport_habits_body(availability={"days": ["MON"] * 8, "time_slots": ["MORNING"] * 5})

        self.assertEqual(
            problems(empty),
            {("availability.days", "required"), ("availability.time_slots", "required")},
        )
        self.assertEqual(
            problems(long),
            {
                ("availability.days", "too_many_items"),
                ("availability.time_slots", "too_many_items"),
            },
        )

    def test_unknown_goal_and_missing_sections(self) -> None:
        self.assertEqual(
            problems(sport_habits_body(primary_goal="FUN")), {("primary_goal", "unknown_value")}
        )
        self.assertEqual(problems({}), {("sports", "required"), ("availability", "required")})
        self.assertEqual(
            problems(sport_habits_body(availability={})),
            {("availability.days", "required"), ("availability.time_slots", "required")},
        )

    def test_expected_version_must_be_positive(self) -> None:
        self.assertEqual(
            problems(sport_habits_body(expected_version=0)), {("expected_version", "out_of_range")}
        )


class SportHabitsTransportTest(unittest.TestCase):
    """Forma del JSON: campos de más (incluido `user_id`) y tipos incorrectos."""

    def test_fields_not_allowed_at_every_level(self) -> None:
        body = sport_habits_body(user_id="00000000-0000-4000-8000-000000000000")
        body["sports"] = [sport(calories=500)]
        body["availability"] = {"days": ["MON"], "time_slots": ["MORNING"], "weeks": 2}

        self.assertEqual(
            problems(body),
            {
                ("user_id", "field_not_allowed"),
                ("sports[0].calories", "field_not_allowed"),
                ("availability.weeks", "field_not_allowed"),
            },
        )

    def test_wrong_json_types_are_unknown_values(self) -> None:
        cases: list[tuple[dict[str, Any], set[tuple[str | None, str]]]] = [
            ({"sports": "running"}, {("sports", "unknown_value")}),
            ({"sports": ["running"]}, {("sports[0]", "unknown_value")}),
            ({"sports": [None]}, {("sports[0]", "unknown_value")}),
            (
                {"sports": [sport(frequency_per_week="3")]},
                {("sports[0].frequency_per_week", "unknown_value")},
            ),
            (
                {"sports": [sport(frequency_per_week=True)]},
                {("sports[0].frequency_per_week", "unknown_value")},
            ),
            ({"availability": ["MON"]}, {("availability", "unknown_value")}),
            (
                {"availability": {"days": "MON", "time_slots": ["NIGHT"]}},
                {("availability.days", "unknown_value")},
            ),
            (
                {"availability": {"days": [1], "time_slots": ["NIGHT"]}},
                {("availability.days[0]", "unknown_value")},
            ),
            ({"primary_goal": 1}, {("primary_goal", "unknown_value")}),
        ]
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                self.assertEqual(problems(sport_habits_body(**overrides)), expected)

    def test_body_must_be_an_object(self) -> None:
        self.assertEqual(problems(None), {(None, "required")})
        self.assertEqual(problems([]), {(None, "unknown_value")})


if __name__ == "__main__":
    unittest.main()
