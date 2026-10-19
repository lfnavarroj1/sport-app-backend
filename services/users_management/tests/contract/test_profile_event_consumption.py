"""El proyector de usuarios consume los esquemas versionados de `sportapp.usuarios.v1`.

Ejemplos válidos de `perfil.demografico_actualizado.v1` y `perfil.fisiologico_actualizado.v1`
cumplen su esquema y el parseo del proyector los acepta; los documentos con campos
excluidos del MVP (ADR-034) o sin `bmi` los rechazan el esquema y el parseo.
"""

import json
import unittest
from pathlib import Path
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker

from sportapp_platform.testing.contracts import repository_root
from users_management.application import messages
from users_management.application.messages import (
    InvalidEvent,
    ProfileSectionUpdatedEvent,
    parse_users_event,
)
from users_management.domain.profile.events import EVENT_TYPE_BY_SECTION, ProfileSection

SCHEMAS = Path("contracts/events/usuarios")


def _validator(event_type: str) -> Draft202012Validator:
    path = repository_root(Path(__file__).resolve()) / SCHEMAS / f"{event_type}.schema.json"
    return Draft202012Validator(
        json.loads(path.read_text(encoding="utf-8")), format_checker=FormatChecker()
    )


def _envelope(section: ProfileSection, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "event_type": EVENT_TYPE_BY_SECTION[section],
        "occurred_at": "2026-10-16T12:00:00Z",
        "producer": "gestion-usuarios",
        "aggregate_id": payload["user_id"],
        "aggregate_version": payload["version"],
        "correlation_id": str(uuid4()),
        "causation_id": str(uuid4()),
        "schema_version": 1,
        "payload": payload,
    }


def demographic_example(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "user_id": str(uuid4()),
        "version": 2,
        "birth_date": "1990-05-14",
        "gender": "NON_BINARY",
        "country_code": "CO",
        "city": "Bogotá",
        "marital_status": "SINGLE",
        "occupation": None,
        "income_range": "1_2_SMMLV",
        "created_at": "2026-10-16T12:00:00Z",
        "updated_at": "2026-10-16T12:30:00.250000Z",
    }
    payload.update(overrides)
    return _envelope(ProfileSection.DEMOGRAPHIC, payload)


def physiological_example(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "user_id": str(uuid4()),
        "version": 1,
        "consent": {"version": "health-test.v1", "accepted_at": "2026-10-16T12:00:00Z"},
        "measured_at": "2026-10-16T07:30:00Z",
        "weight_kg": "68.5",
        "height_cm": 172,
        "bmi": "23.2",
        "resting_heart_rate_bpm": None,
        "blood_pressure": {"systolic_mmhg": 118, "diastolic_mmhg": 76},
        "body_temperature_c": None,
        "created_at": "2026-10-16T12:00:00Z",
        "updated_at": "2026-10-16T12:00:00Z",
    }
    payload.update(overrides)
    return _envelope(ProfileSection.PHYSIOLOGICAL, payload)


EXAMPLES = {
    ProfileSection.DEMOGRAPHIC: demographic_example,
    ProfileSection.PHYSIOLOGICAL: physiological_example,
}


class ProfileEventConsumptionContractTest(unittest.TestCase):
    def test_valid_examples_match_the_schema_and_are_parsed(self) -> None:
        """CA7: el proyector acepta lo que el contrato publica."""
        for section, example in EXAMPLES.items():
            with self.subTest(section=section):
                envelope = example()
                _validator(EVENT_TYPE_BY_SECTION[section]).validate(envelope)

                event = parse_users_event(envelope)

                assert isinstance(event, ProfileSectionUpdatedEvent)
                self.assertEqual(event.section, section)
                self.assertNotIn("user_id", event.document)

    def test_excluded_fields_are_rejected_by_schema_and_parser(self) -> None:
        """ADR-034: raza/etnia y religión quedan fuera del MVP; tampoco en el evento."""
        for section, example in EXAMPLES.items():
            for excluded in ("race", "religion", "ethnicity"):
                with self.subTest(section=section, field=excluded):
                    envelope = example(**{excluded: "X"})

                    self.assertFalse(_validator(EVENT_TYPE_BY_SECTION[section]).is_valid(envelope))
                    with self.assertRaises(InvalidEvent):
                        parse_users_event(envelope)

    def test_physiological_without_bmi_is_rejected(self) -> None:
        envelope = physiological_example()
        del envelope["payload"]["bmi"]

        self.assertFalse(
            _validator(EVENT_TYPE_BY_SECTION[ProfileSection.PHYSIOLOGICAL]).is_valid(envelope)
        )
        with self.assertRaises(InvalidEvent):
            parse_users_event(envelope)

    def test_numeric_decimals_are_rejected_by_schema_and_parser(self) -> None:
        """Los decimales viajan como cadena con un decimal (ADR-034)."""
        envelope = physiological_example(weight_kg=68.5)

        self.assertFalse(
            _validator(EVENT_TYPE_BY_SECTION[ProfileSection.PHYSIOLOGICAL]).is_valid(envelope)
        )
        with self.assertRaises(InvalidEvent):
            parse_users_event(envelope)

    def test_parser_catalogs_match_the_schema(self) -> None:
        schema = _validator(EVENT_TYPE_BY_SECTION[ProfileSection.DEMOGRAPHIC]).schema
        properties = schema["properties"]["payload"]["properties"]

        def enum(name: str) -> set[str]:
            definition = properties[name]
            options = definition.get("anyOf", [definition])
            return next(set(option["enum"]) for option in options if "enum" in option)

        self.assertEqual(enum("gender"), messages.GENDERS)
        self.assertEqual(enum("marital_status"), messages.MARITAL_STATUSES)
        self.assertEqual(enum("income_range"), messages.INCOME_RANGES)
        self.assertEqual(set(properties), messages.DEMOGRAPHIC_FIELDS)
        physiological = _validator(EVENT_TYPE_BY_SECTION[ProfileSection.PHYSIOLOGICAL]).schema
        self.assertEqual(
            set(physiological["properties"]["payload"]["properties"]),
            messages.PHYSIOLOGICAL_FIELDS,
        )


if __name__ == "__main__":
    unittest.main()
