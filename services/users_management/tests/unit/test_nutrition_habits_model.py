"""Reglas de la sección de hábitos alimenticios (HU009, ADR-035) sin infraestructura.

Catálogos cerrados, listas sin repetir y posiblemente vacías, límites, decimal de hidratación,
consentimiento ausente o distinto, validación todo o nada y orden canónico de las listas.
"""

import unittest
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fakes import CONSENT_VERSION

from users_management.domain.profile.nutrition_habits import (
    Allergy,
    DietPattern,
    NutritionHabitsInput,
    Restriction,
    validate_nutrition_habits,
)
from users_management.domain.profile.validation import InvalidProfileSection, Issue, Violation

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def section(**overrides: Any) -> NutritionHabitsInput:
    values: dict[str, Any] = {
        "consent_version": CONSENT_VERSION,
        "diet_pattern": "VEGETARIAN",
        "restrictions": ("LOW_SUGAR", "GLUTEN_FREE"),
        "allergies": ("SESAME", "PEANUTS"),
        "meals_per_day": 4,
        "hydration_liters_per_day": "2.5",
    }
    values.update(overrides)
    return NutritionHabitsInput(**values)


class NutritionHabitsModelTest(unittest.TestCase):
    def violations(self, value: NutritionHabitsInput) -> list[tuple[str | None, str]]:
        with self.assertRaises(InvalidProfileSection) as raised:
            validate_nutrition_habits(value, CONSENT_VERSION)
        return [(item.field, item.issue.value) for item in raised.exception.violations]

    def test_valid_section_is_normalized_in_catalog_order(self) -> None:
        """CA1: estado normalizado; las listas salen en el orden de su catálogo."""
        valid = validate_nutrition_habits(section(), CONSENT_VERSION)

        self.assertEqual(valid.diet_pattern, DietPattern.VEGETARIAN)
        self.assertEqual(valid.restrictions, (Restriction.GLUTEN_FREE, Restriction.LOW_SUGAR))
        self.assertEqual(valid.allergies, (Allergy.PEANUTS, Allergy.SESAME))
        self.assertEqual(valid.hydration_liters_per_day, Decimal("2.5"))
        self.assertEqual(
            valid.fields(NOW),
            {
                "consent": {"version": CONSENT_VERSION, "accepted_at": "2026-10-24T12:00:00Z"},
                "diet_pattern": "VEGETARIAN",
                "restrictions": ["GLUTEN_FREE", "LOW_SUGAR"],
                "allergies": ["PEANUTS", "SESAME"],
                "meals_per_day": 4,
                "hydration_liters_per_day": "2.5",
            },
        )

    def test_same_information_in_another_order_has_the_same_fingerprint_base(self) -> None:
        """Consistencia: la huella usa el payload normalizado (listas ordenadas)."""
        reordered = section(
            restrictions=("GLUTEN_FREE", "LOW_SUGAR"), allergies=("PEANUTS", "SESAME")
        )

        self.assertEqual(
            validate_nutrition_habits(section(), CONSENT_VERSION).normalized(),
            validate_nutrition_habits(reordered, CONSENT_VERSION).normalized(),
        )
        self.assertNotIn(
            "accepted_at", str(validate_nutrition_habits(section(), CONSENT_VERSION).normalized())
        )

    def test_empty_lists_and_missing_hydration_are_valid(self) -> None:
        valid = validate_nutrition_habits(
            section(restrictions=(), allergies=(), hydration_liters_per_day=None), CONSENT_VERSION
        )

        self.assertEqual((valid.restrictions, valid.allergies), ((), ()))
        self.assertIsNone(valid.fields(NOW)["hydration_liters_per_day"])

    def test_every_catalog_value_is_accepted(self) -> None:
        valid = validate_nutrition_habits(
            section(
                restrictions=tuple(reversed([value.value for value in Restriction])),
                allergies=tuple(value.value for value in Allergy),
            ),
            CONSENT_VERSION,
        )

        self.assertEqual(valid.restrictions, tuple(Restriction))
        self.assertEqual(valid.allergies, tuple(Allergy))
        for pattern in DietPattern:
            with self.subTest(pattern=pattern):
                self.assertEqual(
                    validate_nutrition_habits(
                        section(diet_pattern=pattern.value), CONSENT_VERSION
                    ).diet_pattern,
                    pattern,
                )

    def test_values_outside_the_catalogs(self) -> None:
        """CA2: sin texto libre; la posición recibida identifica el elemento."""
        self.assertEqual(
            self.violations(
                section(
                    diet_pattern="PALEO",
                    restrictions=("GLUTEN_FREE", "SIN_AZUCAR"),
                    allergies=("Maní",),
                )
            ),
            [
                ("diet_pattern", "unknown_value"),
                ("restrictions[1]", "unknown_value"),
                ("allergies[0]", "unknown_value"),
            ],
        )

    def test_repeated_values(self) -> None:
        self.assertEqual(
            self.violations(
                section(restrictions=("KETO", "KETO"), allergies=("MILK", "EGGS", "MILK"))
            ),
            [("restrictions[1]", "duplicate_value"), ("allergies[2]", "duplicate_value")],
        )

    def test_more_items_than_the_catalog(self) -> None:
        self.assertEqual(
            self.violations(
                section(
                    restrictions=(*(value.value for value in Restriction), "KETO"),
                    allergies=(*(value.value for value in Allergy), "SOY"),
                )
            ),
            [("restrictions", "too_many_items"), ("allergies", "too_many_items")],
        )

    def test_meals_per_day_limits(self) -> None:
        for value, accepted in ((0, False), (1, True), (8, True), (9, False)):
            with self.subTest(meals=value):
                if accepted:
                    self.assertEqual(
                        validate_nutrition_habits(
                            section(meals_per_day=value), CONSENT_VERSION
                        ).meals_per_day,
                        value,
                    )
                else:
                    self.assertEqual(
                        self.violations(section(meals_per_day=value)),
                        [("meals_per_day", "out_of_range")],
                    )

    def test_hydration_decimal(self) -> None:
        """CA2: decimal inválido; límites 0.5 y 10.0; `"3"` se normaliza a `"3.0"`."""
        for text, expected in (("0.5", "0.5"), ("10.0", "10.0"), ("3", "3.0"), ("10", "10.0")):
            with self.subTest(accepted=text):
                self.assertEqual(
                    validate_nutrition_habits(
                        section(hydration_liters_per_day=text), CONSENT_VERSION
                    ).fields(NOW)["hydration_liters_per_day"],
                    expected,
                )
        for text, issue in (
            ("0.4", "out_of_range"),
            ("10.1", "out_of_range"),
            ("2.25", "too_many_decimals"),
            ("dos", "unknown_value"),
            ("2,5", "unknown_value"),
            ("-1.0", "out_of_range"),
        ):
            with self.subTest(rejected=text):
                self.assertEqual(
                    self.violations(section(hydration_liters_per_day=text)),
                    [("hydration_liters_per_day", issue)],
                )

    def test_consent_missing_or_other_version(self) -> None:
        """CA2: sin consentimiento vigente no se acepta ningún valor."""
        self.assertEqual(
            self.violations(section(consent_version=None)), [("consent.version", "required")]
        )
        self.assertEqual(
            self.violations(section(consent_version="health-2025.v0")),
            [("consent.version", "unsupported_consent_version")],
        )

    def test_required_fields(self) -> None:
        self.assertEqual(
            self.violations(NutritionHabitsInput(consent_version=CONSENT_VERSION)),
            [
                ("diet_pattern", "required"),
                ("restrictions", "required"),
                ("allergies", "required"),
                ("meals_per_day", "required"),
            ],
        )

    def test_all_violations_are_reported_together(self) -> None:
        """Todo o nada: se informan todas las reglas incumplidas a la vez."""
        self.assertEqual(
            self.violations(
                section(
                    expected_version=0,
                    consent_version="otra",
                    meals_per_day=12,
                    hydration_liters_per_day="20.0",
                )
            ),
            [
                ("expected_version", "out_of_range"),
                ("consent.version", "unsupported_consent_version"),
                ("meals_per_day", "out_of_range"),
                ("hydration_liters_per_day", "out_of_range"),
            ],
        )

    def test_transport_violations_are_not_reported_twice(self) -> None:
        reported = Violation("restrictions", Issue.UNKNOWN_VALUE)

        self.assertEqual(
            self.violations(section(restrictions=None, transport_violations=(reported,))),
            [("restrictions", "unknown_value")],
        )

    def test_repr_never_contains_health_data(self) -> None:
        value = section()
        valid = validate_nutrition_habits(value, CONSENT_VERSION)

        for text in (repr(value), repr(valid)):
            for sensitive in ("PEANUTS", "GLUTEN_FREE", "VEGETARIAN", "2.5"):
                self.assertNotIn(sensitive, text)


if __name__ == "__main__":
    unittest.main()
