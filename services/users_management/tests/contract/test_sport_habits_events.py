"""HU006 (ADR-034): `habitos.deportivos_actualizados.v1` producido por el comando real cumple
su esquema versionado, el proyector acepta lo que el contrato publica y los catálogos del
parser coinciden con los del esquema."""

import json
import unittest
from pathlib import Path
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from test_events import _CapturingUnitOfWork, _claimed, _Clock

from sportapp_platform.testing.contracts import repository_root
from users_management.api.profile_payload import parse_sport_habits
from users_management.application.commands.profile_section import ProfileSectionCommand
from users_management.application.commands.update_sport_habits import UpdateSportHabits
from users_management.application.messages import (
    InvalidEvent,
    ProfileSectionUpdatedEvent,
    parse_users_event,
)
from users_management.application.ports import OutboxMessage, StoredProfileSection
from users_management.domain.profile.events import ProfileSection
from users_management.domain.profile.sport_habits import (
    Day,
    Intensity,
    Level,
    PrimaryGoal,
    Sport,
    TimeSlot,
)
from users_management.workers.outbox_publisher import build_envelope

EVENT_TYPE = "habitos.deportivos_actualizados.v1"
BODY: dict[str, Any] = {
    "sports": [
        {
            "sport": "cycling",
            "frequency_per_week": 2,
            "session_duration_minutes": 90,
            "intensity": "HIGH",
            "level": "ADVANCED",
        },
        {
            "sport": "running",
            "frequency_per_week": 3,
            "session_duration_minutes": 45,
            "intensity": "MODERATE",
            "level": "INTERMEDIATE",
            "years_practicing": 4,
        },
    ],
    "availability": {"days": ["SUN", "MON"], "time_slots": ["NIGHT", "MORNING"]},
    "primary_goal": "PERFORMANCE",
}


def validator() -> Draft202012Validator:
    path = repository_root(Path(__file__).resolve()) / (
        f"contracts/events/usuarios/{EVENT_TYPE}.schema.json"
    )
    return Draft202012Validator(
        json.loads(path.read_text(encoding="utf-8")), format_checker=FormatChecker()
    )


def produce(body: dict[str, Any], current: StoredProfileSection | None = None) -> dict[str, Any]:
    """Envelope que publicaría `usuarios-outbox-publisher` para el comando real."""
    outbox: list[OutboxMessage] = []
    use_case = UpdateSportHabits(
        unit_of_work=lambda: _CapturingUnitOfWork(current, outbox), clock=_Clock()
    )
    use_case(uuid4(), ProfileSectionCommand(parse_sport_habits(body), uuid4(), uuid4()))
    [message] = outbox
    loaded: dict[str, Any] = json.loads(json.dumps(build_envelope(_claimed(message))))
    return loaded


class SportHabitsEventContractTest(unittest.TestCase):
    def test_created_and_replaced_events_match_the_schema(self) -> None:
        """CA3: estado completo de la sección, una versión por comando."""
        created = produce(BODY)
        current = StoredProfileSection(
            section=ProfileSection.SPORT_HABITS,
            user_id=uuid4(),
            version=2,
            document={"version": 2, "created_at": "2026-10-01T08:00:00Z"},
        )
        minimal = {**BODY, "primary_goal": None, "expected_version": 2}
        replaced = produce(minimal, current)

        for envelope in (created, replaced):
            validator().validate(envelope)
            self.assertEqual(envelope["payload"]["version"], envelope["aggregate_version"])
            self.assertEqual(envelope["payload"]["user_id"], envelope["aggregate_id"])
        self.assertEqual(created["payload"]["availability"]["days"], ["MON", "SUN"])
        self.assertEqual([s["sport"] for s in created["payload"]["sports"]], ["running", "cycling"])
        self.assertEqual(replaced["aggregate_version"], 3)
        self.assertEqual(replaced["payload"]["created_at"], "2026-10-01T08:00:00Z")

    def test_published_event_is_accepted_by_the_projector(self) -> None:
        event = parse_users_event(produce(BODY))

        assert isinstance(event, ProfileSectionUpdatedEvent)
        self.assertEqual(event.section, ProfileSection.SPORT_HABITS)

    def test_demographic_or_physiological_data_are_rejected(self) -> None:
        """CA3: el evento no transporta datos demográficos ni fisiológicos."""
        envelope = produce(BODY)
        for extra in ({"birth_date": "1992-04-18"}, {"weight_kg": "68.5"}, {"bmi": "23.2"}):
            with self.subTest(extra=extra):
                tampered = {**envelope, "payload": {**envelope["payload"], **extra}}
                self.assertFalse(validator().is_valid(tampered))
                with self.assertRaises(InvalidEvent):
                    parse_users_event(tampered)

    def test_parser_catalogs_match_the_schema(self) -> None:
        payload = validator().schema["properties"]["payload"]["properties"]
        sport = payload["sports"]["items"]["properties"]
        availability = payload["availability"]["properties"]

        for schema_values, catalog in (
            (sport["sport"]["enum"], Sport),
            (sport["intensity"]["enum"], Intensity),
            (sport["level"]["enum"], Level),
            (availability["days"]["items"]["enum"], Day),
            (availability["time_slots"]["items"]["enum"], TimeSlot),
            ([v for v in payload["primary_goal"]["enum"] if v is not None], PrimaryGoal),
        ):
            with self.subTest(catalog=catalog.__name__):
                self.assertEqual(schema_values, [member.value for member in catalog])


if __name__ == "__main__":
    unittest.main()
