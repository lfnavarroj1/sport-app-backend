"""Reglas de las secciones del perfil (HU005, ADR-034) sin infraestructura."""

import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fakes import CONSENT_VERSION, demographic_body, physiological_body

from users_management.api.profile_payload import parse_demographic, parse_physiological
from users_management.domain.profile.countries import COUNTRY_CODES
from users_management.domain.profile.demographic import (
    DemographicSection,
    age_on,
    validate_demographic,
)
from users_management.domain.profile.physiological import (
    PhysiologicalSection,
    body_mass_index,
    validate_physiological,
)
from users_management.domain.profile.section import VersionConflict, next_version
from users_management.domain.profile.validation import InvalidProfileSection

TODAY = date(2026, 10, 7)
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def demographic(**overrides: Any) -> DemographicSection:
    return validate_demographic(parse_demographic(demographic_body(**overrides)), TODAY)


def physiological(**overrides: Any) -> PhysiologicalSection:
    return validate_physiological(
        parse_physiological(physiological_body(**overrides)), NOW, CONSENT_VERSION
    )


class ProfileValidationCase(unittest.TestCase):
    def assertViolations(  # noqa: N802 - estilo de unittest
        self, call: Any, expected: list[tuple[str | None, str]]
    ) -> None:
        with self.assertRaises(InvalidProfileSection) as caught:
            call()
        found = [(item.field, item.issue.value) for item in caught.exception.violations]
        self.assertEqual(found, expected)


class DemographicSectionTest(ProfileValidationCase):
    def test_normalizes_the_adr_example(self) -> None:
        """CA1: catálogos válidos, ciudad y ocupación recortadas."""
        section = demographic(city="  Bogotá ", occupation=" Ingeniera  ")

        self.assertEqual(
            section.fields(NOW),
            {
                "birth_date": "1992-04-18",
                "gender": "FEMALE",
                "country_code": "CO",
                "city": "Bogotá",
                "marital_status": "SINGLE",
                "occupation": "Ingeniera",
                "income_range": "2_4_SMMLV",
            },
        )

    def test_optional_fields_absent_or_null_are_null(self) -> None:
        body = demographic_body(occupation=None)
        del body["marital_status"], body["income_range"]

        fields = validate_demographic(parse_demographic(body), TODAY).fields(NOW)

        self.assertIsNone(fields["marital_status"])
        self.assertIsNone(fields["occupation"])
        self.assertIsNone(fields["income_range"])

    def test_every_catalog_value_is_accepted(self) -> None:
        """CA1: catálogos exactos de ADR-034."""
        catalogs = {
            "gender": ["FEMALE", "MALE", "NON_BINARY", "PREFER_NOT_TO_SAY"],
            "marital_status": [
                "SINGLE",
                "MARRIED",
                "DOMESTIC_PARTNERSHIP",
                "DIVORCED",
                "WIDOWED",
                "PREFER_NOT_TO_SAY",
            ],
            "income_range": [
                "LT_1_SMMLV",
                "1_2_SMMLV",
                "2_4_SMMLV",
                "GT_4_SMMLV",
                "PREFER_NOT_TO_SAY",
            ],
        }
        for field, values in catalogs.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assertEqual(demographic(**{field: value}).fields(NOW)[field], value)

    def test_unknown_catalog_values(self) -> None:
        """CA2: catálogo desconocido → `unknown_value` (también en minúsculas)."""
        for field, value in (
            ("gender", "female"),
            ("gender", "OTHER"),
            ("marital_status", "ENGAGED"),
            ("income_range", "5_SMMLV"),
            ("country_code", "co"),
            ("country_code", "XX"),
            ("country_code", "COL"),
        ):
            with self.subTest(field=field, value=value):
                self.assertViolations(
                    lambda f=field, v=value: demographic(**{f: v}), [(field, "unknown_value")]
                )

    def test_country_catalog_is_the_adr_028_catalog(self) -> None:
        self.assertEqual(len(COUNTRY_CODES), 249)
        self.assertIn("CO", COUNTRY_CODES)

    def test_text_length_after_trimming(self) -> None:
        """CA2: ciudad y ocupación de 2 a 80 caracteres recortados."""
        self.assertEqual(demographic(city="  Cú ").city, "Cú")
        self.assertEqual(demographic(city="x" * 80).city, "x" * 80)
        for field, value in (("city", " a "), ("city", "x" * 81), ("occupation", "  ")):
            with self.subTest(field=field, value=value):
                self.assertViolations(
                    lambda f=field, v=value: demographic(**{f: v}), [(field, "invalid_length")]
                )

    def test_age_limits_on_the_command_utc_date(self) -> None:
        """CA2: edad entre 13 y 120 años, ambos incluidos, en la fecha del comando."""
        self.assertEqual(demographic(birth_date="2013-10-07").birth_date, date(2013, 10, 7))
        self.assertEqual(demographic(birth_date="1906-10-07").birth_date, date(1906, 10, 7))
        # Nacido el 1905-10-08 tiene 120 años; el 1905-10-07 ya cumplió 121.
        self.assertEqual(demographic(birth_date="1905-10-08").birth_date, date(1905, 10, 8))
        for value in ("2013-10-08", "1905-10-07", "2030-01-01"):
            with self.subTest(birth_date=value):
                self.assertViolations(
                    lambda v=value: demographic(birth_date=v), [("birth_date", "out_of_range")]
                )

    def test_age_counts_completed_years(self) -> None:
        self.assertEqual(age_on(date(2000, 2, 29), date(2013, 2, 28)), 12)
        self.assertEqual(age_on(date(2000, 2, 29), date(2013, 3, 1)), 13)

    def test_invalid_dates(self) -> None:
        for value in ("2026-02-30", "18/04/1992", "19920418", "1992-04-18T00:00:00Z", ""):
            with self.subTest(birth_date=value):
                self.assertViolations(
                    lambda v=value: demographic(birth_date=v), [("birth_date", "invalid_date")]
                )

    def test_required_fields(self) -> None:
        self.assertViolations(
            lambda: validate_demographic(parse_demographic({}), TODAY),
            [
                ("birth_date", "required"),
                ("gender", "required"),
                ("country_code", "required"),
                ("city", "required"),
            ],
        )

    def test_reports_every_violation_together(self) -> None:
        """CA2: todo o nada, con todas las violaciones."""
        self.assertViolations(
            lambda: demographic(user_id="x", gender="X", city=7, expected_version=0),
            [
                ("user_id", "field_not_allowed"),
                ("city", "unknown_value"),
                ("expected_version", "out_of_range"),
                ("gender", "unknown_value"),
            ],
        )

    def test_body_must_be_an_object(self) -> None:
        self.assertViolations(
            lambda: validate_demographic(parse_demographic(None), TODAY), [(None, "required")]
        )
        self.assertViolations(
            lambda: validate_demographic(parse_demographic([1]), TODAY),
            [(None, "unknown_value")],
        )

    def test_expected_version_must_be_a_positive_integer(self) -> None:
        self.assertEqual(demographic(expected_version=3).expected_version, 3)
        self.assertViolations(
            lambda: demographic(expected_version="1"), [("expected_version", "unknown_value")]
        )
        self.assertViolations(
            lambda: demographic(expected_version=True), [("expected_version", "unknown_value")]
        )

    def test_repr_has_no_personal_data(self) -> None:
        section = demographic()
        self.assertNotIn("Bogotá", repr(section))
        self.assertNotIn("1992", repr(parse_demographic(demographic_body())))


class PhysiologicalSectionTest(ProfileValidationCase):
    def test_normalizes_the_adr_example_with_bmi(self) -> None:
        """CA1: decimales con un decimal y `bmi` = 68.5 / 1.72² → 23.2."""
        section = physiological()

        self.assertEqual(
            section.fields(NOW),
            {
                "consent": {"version": CONSENT_VERSION, "accepted_at": "2026-10-07T12:00:00Z"},
                "measured_at": "2026-10-07T11:00:00Z",
                "weight_kg": "68.5",
                "height_cm": 172,
                "bmi": "23.2",
                "resting_heart_rate_bpm": 58,
                "blood_pressure": {"systolic_mmhg": 118, "diastolic_mmhg": 76},
                "body_temperature_c": "36.6",
            },
        )
        self.assertNotIn("accepted_at", str(section.normalized()))

    def test_body_mass_index_rounds_half_up(self) -> None:
        self.assertEqual(body_mass_index(Decimal("68.5"), 172), Decimal("23.2"))
        # 90.6 / 2.0² = 22.65 exacto: ROUND_HALF_UP da 22.7 (el redondeo bancario, 22.6).
        self.assertEqual(body_mass_index(Decimal("22.5"), 100), Decimal("22.5"))
        self.assertEqual(body_mass_index(Decimal("81.0"), 180), Decimal("25.0"))
        self.assertEqual(body_mass_index(Decimal("50.6"), 150), Decimal("22.5"))
        self.assertEqual(body_mass_index(Decimal("50.7"), 150), Decimal("22.5"))
        self.assertEqual(body_mass_index(Decimal("90.6"), 200), Decimal("22.7"))

    def test_optional_fields_absent_are_null(self) -> None:
        body = physiological_body()
        for name in ("resting_heart_rate_bpm", "blood_pressure", "body_temperature_c"):
            del body[name]

        fields = validate_physiological(parse_physiological(body), NOW, CONSENT_VERSION).fields(NOW)

        self.assertIsNone(fields["resting_heart_rate_bpm"])
        self.assertIsNone(fields["blood_pressure"])
        self.assertIsNone(fields["body_temperature_c"])

    def test_decimals_are_normalized_to_one_decimal(self) -> None:
        section = physiological(weight_kg="68", body_temperature_c="37")
        self.assertEqual(section.fields(NOW)["weight_kg"], "68.0")
        self.assertEqual(section.fields(NOW)["body_temperature_c"], "37.0")

    def test_decimal_rules(self) -> None:
        """CA2: cadena decimal, un decimal como máximo y rango."""
        cases = [
            ("weight_kg", 68.5, "unknown_value"),
            ("weight_kg", "68,5", "unknown_value"),
            ("weight_kg", "6.85e1", "unknown_value"),
            ("weight_kg", "68.55", "too_many_decimals"),
            ("weight_kg", "19.9", "out_of_range"),
            ("weight_kg", "300.1", "out_of_range"),
            ("weight_kg", "-70.0", "out_of_range"),
            ("body_temperature_c", 36.6, "unknown_value"),
            ("body_temperature_c", "36.66", "too_many_decimals"),
            ("body_temperature_c", "33.9", "out_of_range"),
            ("body_temperature_c", "42.1", "out_of_range"),
        ]
        for field, value, issue in cases:
            with self.subTest(field=field, value=value):
                self.assertViolations(
                    lambda f=field, v=value: physiological(**{f: v}), [(field, issue)]
                )

    def test_decimal_limits_are_inclusive(self) -> None:
        self.assertEqual(physiological(weight_kg="20.0").weight_kg, Decimal("20.0"))
        self.assertEqual(physiological(weight_kg="300.0").weight_kg, Decimal("300.0"))
        self.assertEqual(
            physiological(body_temperature_c="34.0").fields(NOW)["body_temperature_c"], "34.0"
        )
        self.assertEqual(
            physiological(body_temperature_c="42.0").fields(NOW)["body_temperature_c"], "42.0"
        )

    def test_integer_ranges(self) -> None:
        """CA2: altura, frecuencia y presión dentro de sus rangos (límites incluidos)."""
        accepted = [
            ("height_cm", 100),
            ("height_cm", 250),
            ("resting_heart_rate_bpm", 30),
            ("resting_heart_rate_bpm", 220),
        ]
        for field, value in accepted:
            with self.subTest(field=field, value=value):
                physiological(**{field: value})
        rejected = [
            ("height_cm", 99, "out_of_range"),
            ("height_cm", 251, "out_of_range"),
            ("height_cm", "172", "unknown_value"),
            ("height_cm", 172.5, "unknown_value"),
            ("resting_heart_rate_bpm", 29, "out_of_range"),
            ("resting_heart_rate_bpm", 221, "out_of_range"),
        ]
        for field, value, issue in rejected:
            with self.subTest(field=field, value=value):
                self.assertViolations(
                    lambda f=field, v=value: physiological(**{f: v}), [(field, issue)]
                )

    def test_blood_pressure_ranges_and_consistency(self) -> None:
        """CA2: sistólica 60 a 250, diastólica 30 a 150 y sistólica > diastólica."""
        physiological(blood_pressure={"systolic_mmhg": 250, "diastolic_mmhg": 150})
        physiological(blood_pressure={"systolic_mmhg": 60, "diastolic_mmhg": 30})
        cases = [
            (
                {"systolic_mmhg": 59, "diastolic_mmhg": 40},
                [("blood_pressure.systolic_mmhg", "out_of_range")],
            ),
            (
                {"systolic_mmhg": 120, "diastolic_mmhg": 151},
                [("blood_pressure.diastolic_mmhg", "out_of_range")],
            ),
            (
                {"systolic_mmhg": 80, "diastolic_mmhg": 80},
                [("blood_pressure", "inconsistent_values")],
            ),
            (
                {"systolic_mmhg": 76, "diastolic_mmhg": 118},
                [("blood_pressure", "inconsistent_values")],
            ),
            ({"systolic_mmhg": 118}, [("blood_pressure.diastolic_mmhg", "required")]),
            (
                {"systolic_mmhg": 118, "diastolic_mmhg": 76, "pulse": 1},
                [("blood_pressure.pulse", "field_not_allowed")],
            ),
            ("118/76", [("blood_pressure", "unknown_value")]),
        ]
        for value, expected in cases:
            with self.subTest(blood_pressure=value):
                self.assertViolations(lambda v=value: physiological(blood_pressure=v), expected)

    def test_measured_at_window(self) -> None:
        """CA2: hasta 5 minutos en el futuro y 365 días en el pasado."""
        physiological(measured_at="2026-10-07T12:05:00Z")
        physiological(measured_at="2025-10-07T12:00:00Z")
        cases = [
            ("2026-10-07T12:05:01Z", "too_far_in_future"),
            ("2025-10-07T11:59:59Z", "too_old"),
            ("2026-10-07T11:00:00.500Z", "invalid_datetime"),
            ("2026-10-07T06:00:00-05:00", "invalid_datetime"),
            ("2026-10-07 11:00:00Z", "invalid_datetime"),
            ("2026-13-07T11:00:00Z", "invalid_datetime"),
        ]
        for value, issue in cases:
            with self.subTest(measured_at=value):
                self.assertViolations(
                    lambda v=value: physiological(measured_at=v), [("measured_at", issue)]
                )

    def test_consent_is_required_and_must_be_current(self) -> None:
        """CA3: consentimiento ausente → `required`; distinto → `unsupported_consent_version`."""
        body = physiological_body()
        del body["consent"]
        self.assertViolations(
            lambda: validate_physiological(parse_physiological(body), NOW, CONSENT_VERSION),
            [("consent.version", "required")],
        )
        self.assertViolations(lambda: physiological(consent={}), [("consent.version", "required")])
        self.assertViolations(
            lambda: physiological(consent={"version": "health-2020.v0"}),
            [("consent.version", "unsupported_consent_version")],
        )
        self.assertViolations(lambda: physiological(consent="yes"), [("consent", "unknown_value")])
        self.assertViolations(
            lambda: physiological(consent={"version": CONSENT_VERSION, "accepted": True}),
            [("consent.accepted", "field_not_allowed")],
        )

    def test_bmi_and_user_id_are_not_allowed_in_the_body(self) -> None:
        """CA2: `bmi` lo calcula el backend; `user_id` sale del token."""
        self.assertViolations(
            lambda: physiological(bmi="23.2", user_id="x"),
            [("bmi", "field_not_allowed"), ("user_id", "field_not_allowed")],
        )

    def test_required_fields(self) -> None:
        self.assertViolations(
            lambda: validate_physiological(parse_physiological({}), NOW, CONSENT_VERSION),
            [
                ("consent.version", "required"),
                ("measured_at", "required"),
                ("weight_kg", "required"),
                ("height_cm", "required"),
            ],
        )

    def test_violations_never_contain_values(self) -> None:
        with self.assertRaises(InvalidProfileSection) as caught:
            physiological(weight_kg="301.5", body_temperature_c="44.4")
        self.assertNotIn("301.5", str(caught.exception))
        self.assertNotIn("44.4", str(caught.exception))
        self.assertNotIn("68.5", repr(physiological()))
        self.assertNotIn("68.5", repr(parse_physiological(physiological_body())))


class VersionRuleTest(unittest.TestCase):
    def test_versions(self) -> None:
        """CA6: alta sin versión; reemplazo con la versión vigente; otro caso → conflicto."""
        self.assertEqual(next_version(None, None), 1)
        self.assertEqual(next_version(3, 3), 4)
        for expected, current, reported in ((None, 3, 3), (2, 3, 3), (1, None, 0)):
            with self.subTest(expected=expected, current=current):
                with self.assertRaises(VersionConflict) as caught:
                    next_version(expected, current)
                self.assertEqual(caught.exception.current_version, reported)

    def test_measured_at_respects_command_instant(self) -> None:
        later = NOW + timedelta(days=366)
        with self.assertRaises(InvalidProfileSection):
            validate_physiological(
                parse_physiological(physiological_body()), later, CONSENT_VERSION
            )


if __name__ == "__main__":
    unittest.main()
