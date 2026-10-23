"""Parseo y validación de los eventos de `sportapp.usuarios.v1` (CMP-001, ADR-034)."""

import unittest
from typing import Any
from uuid import uuid4

from projection_fakes import (
    demographic_payload,
    physiological_payload,
    profile_envelope,
    registered_envelope,
)

from users_management.application.messages import (
    InvalidEvent,
    ProfileSectionUpdatedEvent,
    UnsupportedEvent,
    UserRegisteredEvent,
    parse_users_event,
)
from users_management.domain.profile.events import ProfileSection


class ParseUserRegisteredTest(unittest.TestCase):
    def test_valid_registration(self) -> None:
        user_id = uuid4()
        event = parse_users_event(registered_envelope(user_id, email="ana@example.com"))

        self.assertIsInstance(event, UserRegisteredEvent)
        assert isinstance(event, UserRegisteredEvent)
        self.assertEqual(event.user_id, user_id)
        self.assertEqual(event.aggregate_id, user_id)
        self.assertEqual(event.email, "ana@example.com")
        self.assertEqual(
            (event.actor_type, event.status, event.aggregate_version), ("athlete", "active", 1)
        )
        self.assertNotIn("ana@example.com", repr(event), "el correo no aparece en trazas")

    def test_contract_violations(self) -> None:
        user_id = uuid4()
        base = registered_envelope(user_id)["payload"]
        cases: dict[str, Any] = {
            "aggregate_version_invalid": registered_envelope(user_id, aggregate_version=2),
            "aggregate_id_mismatch": registered_envelope(user_id, aggregate_id=str(uuid4())),
            "payload_fields_mismatch": registered_envelope(
                user_id, payload={**base, "password_hash": "x"}
            ),
            "unknown_actor_type": registered_envelope(
                user_id, payload={**base, "actor_type": "admin"}
            ),
            "unknown_status": registered_envelope(user_id, payload={**base, "status": "disabled"}),
            "email_invalid": registered_envelope(user_id, payload={**base, "email": "a"}),
            "full_name_invalid": registered_envelope(user_id, payload={**base, "full_name": ""}),
            "registered_at_not_datetime": registered_envelope(
                user_id, payload={**base, "registered_at": "ayer"}
            ),
        }
        for reason, body in cases.items():
            with self.subTest(reason=reason), self.assertRaises(InvalidEvent) as raised:
                parse_users_event(body)
            self.assertEqual(raised.exception.reason, reason)


class ParseProfileSectionTest(unittest.TestCase):
    def test_valid_sections_drop_user_id_from_the_document(self) -> None:
        """CA7: la sección se proyecta como el estado completo del comando sin `user_id`."""
        for section in (ProfileSection.DEMOGRAPHIC, ProfileSection.PHYSIOLOGICAL):
            with self.subTest(section=section):
                user_id = uuid4()
                body = profile_envelope(section, user_id, 3)

                event = parse_users_event(body)

                self.assertIsInstance(event, ProfileSectionUpdatedEvent)
                assert isinstance(event, ProfileSectionUpdatedEvent)
                self.assertEqual(event.section, section)
                self.assertEqual((event.user_id, event.aggregate_version), (user_id, 3))
                expected = {k: v for k, v in body["payload"].items() if k != "user_id"}
                self.assertEqual(event.document, expected)
                self.assertNotIn("68.5", repr(event))
                self.assertNotIn("Medell", repr(event))

    def test_optional_fields_accept_null(self) -> None:
        user_id = uuid4()
        demographic = demographic_payload(user_id, occupation=None, income_range=None)
        physiological = physiological_payload(
            user_id, resting_heart_rate_bpm=None, blood_pressure=None, body_temperature_c=None
        )

        parse_users_event(
            profile_envelope(ProfileSection.DEMOGRAPHIC, user_id, payload=demographic)
        )
        parse_users_event(
            profile_envelope(ProfileSection.PHYSIOLOGICAL, user_id, payload=physiological)
        )

    def test_demographic_contract_violations(self) -> None:
        """CA4 (minimización): campos excluidos (raza, religión) no se proyectan."""
        user_id = uuid4()
        base = demographic_payload(user_id)

        def body(**changes: Any) -> dict[str, Any]:
            return profile_envelope(
                ProfileSection.DEMOGRAPHIC, user_id, payload={**base, **changes}
            )

        without_city = {key: value for key, value in base.items() if key != "city"}
        cases: list[tuple[str, dict[str, Any]]] = [
            ("payload_fields_mismatch", body(race="X")),
            ("payload_fields_mismatch", body(religion="X")),
            (
                "payload_fields_mismatch",
                profile_envelope(ProfileSection.DEMOGRAPHIC, user_id, payload=without_city),
            ),
            ("unknown_gender", body(gender="OTHER")),
            ("country_code_invalid", body(country_code="col")),
            ("city_invalid", body(city="M")),
            ("birth_date_invalid", body(birth_date="1990-13-40")),
            ("unknown_marital_status", body(marital_status="COMPLICATED")),
            ("occupation_invalid", body(occupation=7)),
            ("unknown_income_range", body(income_range="RICH")),
            ("version_mismatch", body(version=2)),
            ("aggregate_id_mismatch", body(user_id=str(uuid4()))),
            ("updated_at_invalid", body(updated_at="2026-10-16T12:00:00-05:00")),
        ]
        for reason, envelope in cases:
            with self.subTest(reason=reason), self.assertRaises(InvalidEvent) as raised:
                parse_users_event(envelope)
            self.assertEqual(raised.exception.reason, reason)

    def test_physiological_contract_violations(self) -> None:
        user_id = uuid4()
        base = physiological_payload(user_id)

        def body(**changes: Any) -> dict[str, Any]:
            return profile_envelope(
                ProfileSection.PHYSIOLOGICAL, user_id, payload={**base, **changes}
            )

        no_bmi = {key: value for key, value in base.items() if key != "bmi"}
        cases: dict[str, Any] = {
            "payload_fields_mismatch": profile_envelope(
                ProfileSection.PHYSIOLOGICAL, user_id, payload=no_bmi
            ),
            "weight_kg_invalid": body(weight_kg=68.5),
            "bmi_invalid": body(bmi="23.25"),
            "height_cm_out_of_range": body(height_cm=99),
            "measured_at_invalid": body(measured_at="2026-10-16T07:30:00.5Z"),
            "consent_not_object": body(consent="health-test.v1"),
            "consent_fields_mismatch": body(consent={"version": "v1"}),
            "consent_version_invalid": body(
                consent={"version": "", "accepted_at": "2026-10-16T12:00:00Z"}
            ),
            "resting_heart_rate_bpm_out_of_range": body(resting_heart_rate_bpm=True),
            "blood_pressure_fields_mismatch": body(blood_pressure={"systolic_mmhg": 118}),
            "diastolic_mmhg_out_of_range": body(
                blood_pressure={"systolic_mmhg": 118, "diastolic_mmhg": 151}
            ),
            "body_temperature_c_invalid": body(body_temperature_c="36"),
        }
        for reason, envelope in cases.items():
            with self.subTest(reason=reason), self.assertRaises(InvalidEvent) as raised:
                parse_users_event(envelope)
            self.assertEqual(raised.exception.reason, reason)


class ParseEnvelopeTest(unittest.TestCase):
    def test_envelope_violations(self) -> None:
        section = ProfileSection.DEMOGRAPHIC
        cases: dict[str, Any] = {
            "envelope_not_object": [],
            "event_type_missing": profile_envelope(section, event_type=None),
            "unsupported_schema_version": profile_envelope(section, schema_version=2),
            "producer_mismatch": profile_envelope(section, producer="otro"),
            "event_id_not_uuid": profile_envelope(section, event_id="x"),
            "aggregate_version_invalid": profile_envelope(section, aggregate_version=0),
            "payload_not_object": profile_envelope(section, payload="x"),  # type: ignore[arg-type]
            "occurred_at_without_timezone": profile_envelope(
                section, occurred_at="2026-10-16T12:00:00"
            ),
            "correlation_id_not_uuid": profile_envelope(section, correlation_id="x"),
            "causation_id_invalid": profile_envelope(section, causation_id=5),
        }
        for reason, envelope in cases.items():
            with self.subTest(reason=reason), self.assertRaises(InvalidEvent) as raised:
                parse_users_event(envelope)
            self.assertEqual(raised.exception.reason, reason)

    def test_null_correlation_is_accepted(self) -> None:
        event = parse_users_event(profile_envelope(ProfileSection.DEMOGRAPHIC, correlation_id=None))

        self.assertIsNone(event.correlation_id)

    def test_other_event_types_are_unsupported(self) -> None:
        with self.assertRaises(UnsupportedEvent) as raised:
            parse_users_event(
                profile_envelope(ProfileSection.DEMOGRAPHIC, event_type="evento.no_proyectado.v1")
            )

        self.assertEqual(raised.exception.event_type, "evento.no_proyectado.v1")


if __name__ == "__main__":
    unittest.main()
