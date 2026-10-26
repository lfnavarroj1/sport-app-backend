"""HU008 (ADR-035): edición parcial de los hábitos deportivos sin infraestructura.

Se recorre el camino real del transporte (`parse_sport_habits_patch`) y del dominio: qué
secciones llegaron, sus reglas (las de ADR-034) y la fusión con el estado vigente, que se
revalida completo.
"""

import unittest
from datetime import UTC, datetime
from typing import Any

from users_management.api.profile_payload import parse_sport_habits_patch
from users_management.domain.profile.sport_habits import (
    SportHabitsPatch,
    validate_sport_habits_patch,
)
from users_management.domain.profile.validation import InvalidProfileSection

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
RUNNING: dict[str, Any] = {
    "sport": "running",
    "frequency_per_week": 3,
    "session_duration_minutes": 45,
    "intensity": "MODERATE",
    "level": "INTERMEDIATE",
    "years_practicing": 4,
}
SWIMMING: dict[str, Any] = {
    "sport": "swimming",
    "frequency_per_week": 2,
    "session_duration_minutes": 60,
    "intensity": "LOW",
    "level": "BEGINNER",
    "years_practicing": None,
}
CYCLING: dict[str, Any] = {
    "sport": "cycling",
    "frequency_per_week": 4,
    "session_duration_minutes": 90,
    "intensity": "HIGH",
    "level": "ADVANCED",
}
# Documento vigente de HU006 (normalizado), tal como se guarda y se publica.
CURRENT: dict[str, Any] = {
    "user_id": "00000000-0000-4000-8000-000000000001",
    "version": 2,
    "sports": [RUNNING, SWIMMING],
    "availability": {"days": ["MON", "WED", "SAT"], "time_slots": ["MORNING", "EVENING"]},
    "primary_goal": "HEALTH",
    "created_at": "2026-10-07T12:00:00Z",
    "updated_at": "2026-10-08T12:00:00Z",
}


def validate(body: Any) -> SportHabitsPatch:
    return validate_sport_habits_patch(parse_sport_habits_patch(body))


def merged(body: dict[str, Any]) -> dict[str, Any]:
    return validate({"expected_version": 2, **body}).merge(CURRENT, NOW)


def problems(body: Any) -> set[tuple[str | None, str]]:
    try:
        validate(body)
    except InvalidProfileSection as error:
        return {(item.field, item.issue.value) for item in error.violations}
    raise AssertionError("se esperaba validation_error")


class PatchMergeTest(unittest.TestCase):
    """CA1: cada sección enviada reemplaza a la vigente y las demás se conservan."""

    def test_only_sports_keeps_availability_and_goal(self) -> None:
        fields = merged({"sports": [CYCLING]})

        self.assertEqual(fields["sports"], [{**CYCLING, "years_practicing": None}])
        self.assertEqual(fields["availability"], CURRENT["availability"])
        self.assertEqual(fields["primary_goal"], "HEALTH")

    def test_only_availability_keeps_sports_and_goal(self) -> None:
        fields = merged({"availability": {"days": ["SUN", "TUE"], "time_slots": ["NIGHT"]}})

        self.assertEqual(fields["availability"], {"days": ["TUE", "SUN"], "time_slots": ["NIGHT"]})
        self.assertEqual(fields["sports"], CURRENT["sports"])
        self.assertEqual(fields["primary_goal"], "HEALTH")

    def test_only_primary_goal_keeps_sports_and_availability(self) -> None:
        fields = merged({"primary_goal": "COMPETITION"})

        self.assertEqual(fields["primary_goal"], "COMPETITION")
        self.assertEqual(fields["sports"], CURRENT["sports"])
        self.assertEqual(fields["availability"], CURRENT["availability"])

    def test_null_primary_goal_clears_it(self) -> None:
        self.assertIsNone(merged({"primary_goal": None})["primary_goal"])

    def test_combinations_replace_every_sent_section(self) -> None:
        cases: list[dict[str, Any]] = [
            {"sports": [CYCLING], "primary_goal": "PERFORMANCE"},
            {"sports": [CYCLING], "availability": {"days": ["FRI"], "time_slots": ["NIGHT"]}},
            {"availability": {"days": ["FRI"], "time_slots": ["NIGHT"]}, "primary_goal": None},
            {
                "sports": [CYCLING],
                "availability": {"days": ["FRI"], "time_slots": ["NIGHT"]},
                "primary_goal": "RECREATION",
            },
        ]
        for body in cases:
            with self.subTest(sections=sorted(body)):
                fields = merged(body)
                for name in ("sports", "availability", "primary_goal"):
                    if name not in body:
                        self.assertEqual(fields[name], CURRENT[name])
                if "sports" in body:
                    self.assertEqual([item["sport"] for item in fields["sports"]], ["cycling"])
                if "availability" in body:
                    self.assertEqual(fields["availability"]["days"], ["FRI"])
                if "primary_goal" in body:
                    self.assertEqual(fields["primary_goal"], body["primary_goal"])

    def test_merge_only_returns_section_fields(self) -> None:
        """El documento (identidad, versión, marcas) lo arma el comando."""
        self.assertEqual(
            set(merged({"primary_goal": None})), {"sports", "availability", "primary_goal"}
        )

    def test_sent_lists_are_normalized_like_hu006(self) -> None:
        fields = merged(
            {
                "sports": [SWIMMING, CYCLING, RUNNING],
                "availability": {"days": ["SUN", "MON"], "time_slots": ["NIGHT", "MORNING"]},
            }
        )

        self.assertEqual(
            [item["sport"] for item in fields["sports"]], ["running", "cycling", "swimming"]
        )
        self.assertEqual(
            fields["availability"], {"days": ["MON", "SUN"], "time_slots": ["MORNING", "NIGHT"]}
        )

    def test_merged_aggregate_is_revalidated(self) -> None:
        """Las reglas de ADR-034 se aplican al agregado fusionado, no solo a lo enviado."""
        corrupt = {**CURRENT, "availability": {"days": [], "time_slots": ["MORNING"]}}
        patch = validate({"expected_version": 2, "primary_goal": "HEALTH"})

        with self.assertRaises(InvalidProfileSection) as raised:
            patch.merge(corrupt, NOW)

        self.assertEqual(
            {(item.field, item.issue.value) for item in raised.exception.violations},
            {("availability.days", "required")},
        )

    def test_normalized_payload_only_has_sent_sections(self) -> None:
        """Huella idempotente: deportista + operación + secciones enviadas normalizadas."""
        first = validate({"expected_version": 2, "sports": [SWIMMING, RUNNING]})
        reordered = validate({"expected_version": 2, "sports": [RUNNING, SWIMMING]})

        self.assertEqual(first.normalized(), reordered.normalized())
        self.assertEqual(set(first.normalized()), {"expected_version", "sports"})
        self.assertEqual(
            validate({"expected_version": 2, "primary_goal": None}).normalized(),
            {"expected_version": 2, "primary_goal": None},
        )


class PatchValidationTest(unittest.TestCase):
    """CA5: cuerpo sin secciones o fuera de catálogo → `422 validation_error`."""

    def test_empty_or_sectionless_body_is_required(self) -> None:
        self.assertEqual(problems(None), {(None, "required")})
        self.assertEqual(problems({"expected_version": 1}), {(None, "required")})
        self.assertEqual(problems({}), {(None, "required"), ("expected_version", "required")})

    def test_body_that_is_not_an_object(self) -> None:
        self.assertEqual(problems([]), {(None, "unknown_value")})

    def test_expected_version_is_mandatory_and_positive(self) -> None:
        self.assertEqual(problems({"primary_goal": "HEALTH"}), {("expected_version", "required")})
        self.assertEqual(
            problems({"expected_version": 0, "primary_goal": "HEALTH"}),
            {("expected_version", "out_of_range")},
        )
        self.assertEqual(
            problems({"expected_version": "1", "primary_goal": "HEALTH"}),
            {("expected_version", "unknown_value")},
        )

    def test_sections_sent_as_null_are_required(self) -> None:
        self.assertEqual(
            problems({"expected_version": 1, "sports": None, "availability": None}),
            {("sports", "required"), ("availability", "required")},
        )

    def test_catalogs_limits_and_uniqueness_of_adr_034(self) -> None:
        cases: list[tuple[dict[str, Any], set[tuple[str | None, str]]]] = [
            ({"primary_goal": "FUN"}, {("primary_goal", "unknown_value")}),
            ({"sports": []}, {("sports", "required")}),
            ({"sports": [RUNNING] * 6}, {("sports", "too_many_items")}),
            ({"sports": [RUNNING, RUNNING]}, {("sports[1].sport", "duplicate_value")}),
            ({"sports": [{**RUNNING, "sport": "golf"}]}, {("sports[0].sport", "unknown_value")}),
            (
                {"sports": [{**RUNNING, "frequency_per_week": 8}]},
                {("sports[0].frequency_per_week", "out_of_range")},
            ),
            (
                {"sports": [{**RUNNING, "session_duration_minutes": 5}]},
                {("sports[0].session_duration_minutes", "out_of_range")},
            ),
            (
                {"sports": [{k: v for k, v in RUNNING.items() if k != "level"}]},
                {("sports[0].level", "required")},
            ),
            (
                {"availability": {"days": ["MON", "MON"], "time_slots": ["MORNING"]}},
                {("availability.days[1]", "duplicate_value")},
            ),
            (
                {"availability": {"days": ["MON"], "time_slots": []}},
                {("availability.time_slots", "required")},
            ),
            ({"availability": {"days": ["MON"]}}, {("availability.time_slots", "required")}),
            (
                {"availability": {"days": ["XYZ"], "time_slots": ["MORNING"]}},
                {("availability.days[0]", "unknown_value")},
            ),
            ({"availability": []}, {("availability", "unknown_value")}),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(problems({"expected_version": 1, **body}), expected)

    def test_all_violations_are_reported_together(self) -> None:
        """Todo o nada: una sección válida no se acepta si otra es inválida."""
        self.assertEqual(
            problems(
                {
                    "expected_version": 1,
                    "sports": [CYCLING],
                    "primary_goal": "FUN",
                    "availability": {"days": ["MON"], "time_slots": ["DAWN"]},
                }
            ),
            {("primary_goal", "unknown_value"), ("availability.time_slots[0]", "unknown_value")},
        )

    def test_fields_outside_the_contract_are_not_allowed(self) -> None:
        self.assertEqual(
            problems({"expected_version": 1, "primary_goal": None, "user_id": "x", "bmi": "1"}),
            {("user_id", "field_not_allowed"), ("bmi", "field_not_allowed")},
        )

    def test_repr_never_contains_habits(self) -> None:
        patch = validate({"expected_version": 1, "sports": [CYCLING], "primary_goal": "HEALTH"})
        raw = parse_sport_habits_patch({"expected_version": 1, "primary_goal": "HEALTH"})

        for text in (repr(patch), repr(raw)):
            self.assertNotIn("cycling", text)
            self.assertNotIn("HEALTH", text)


if __name__ == "__main__":
    unittest.main()
