"""HU009 (ADR-035): `habitos.alimenticios_actualizados.v1` producido por el comando real cumple su
esquema versionado, el proyector acepta lo que el contrato publica, los catálogos coinciden en el
dominio, el esquema y el OpenAPI, y el contrato interno de restricciones expone solo lo
autorizado."""

import json
import unittest
from pathlib import Path
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from test_events import _CapturingUnitOfWork, _claimed, _Clock

from sportapp_platform.testing.contracts import repository_root
from users_management.api.profile_payload import parse_nutrition_habits
from users_management.application.commands.profile_section import ProfileSectionCommand
from users_management.application.commands.update_nutrition_habits import UpdateNutritionHabits
from users_management.application.messages import (
    InvalidEvent,
    ProfileSectionUpdatedEvent,
    parse_users_event,
)
from users_management.application.ports import OutboxMessage, StoredProfileSection
from users_management.domain.profile.events import ProfileSection
from users_management.domain.profile.nutrition_habits import Allergy, DietPattern, Restriction
from users_management.workers.outbox_publisher import build_envelope

EVENT_TYPE = "habitos.alimenticios_actualizados.v1"
CONSENT = "health-contract.v1"
BODY: dict[str, Any] = {
    "consent": {"version": CONSENT},
    "diet_pattern": "PESCATARIAN",
    "restrictions": ["KOSHER", "LACTOSE_FREE"],
    "allergies": ["SHELLFISH", "TREE_NUTS"],
    "meals_per_day": 5,
    "hydration_liters_per_day": "3",
}
ROOT = repository_root(Path(__file__).resolve())
INTERNAL = "/v1/users/{user_id}/nutrition-restrictions"


def validator() -> Draft202012Validator:
    path = ROOT / f"contracts/events/usuarios/{EVENT_TYPE}.schema.json"
    return Draft202012Validator(
        json.loads(path.read_text(encoding="utf-8")), format_checker=FormatChecker()
    )


def produce(body: dict[str, Any], current: StoredProfileSection | None = None) -> dict[str, Any]:
    """Envelope que publicaría `usuarios-outbox-publisher` para el comando real."""
    outbox: list[OutboxMessage] = []
    use_case = UpdateNutritionHabits(
        unit_of_work=lambda: _CapturingUnitOfWork(current, outbox),
        clock=_Clock(),
        current_consent_version=CONSENT,
    )
    use_case(uuid4(), ProfileSectionCommand(parse_nutrition_habits(body), uuid4(), uuid4()))
    [message] = outbox
    loaded: dict[str, Any] = json.loads(json.dumps(build_envelope(_claimed(message))))
    return loaded


def openapi() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(
        (ROOT / "contracts/openapi/users-management.openapi.json").read_text(encoding="utf-8")
    )
    return document


class NutritionHabitsEventContractTest(unittest.TestCase):
    def test_created_and_replaced_events_match_the_schema(self) -> None:
        """CA3: estado completo de la sección, una versión por comando."""
        created = produce(BODY)
        current = StoredProfileSection(
            section=ProfileSection.NUTRITION_HABITS,
            user_id=uuid4(),
            version=2,
            document={"version": 2, "created_at": "2026-10-01T08:00:00Z"},
        )
        minimal = {
            **BODY,
            "restrictions": [],
            "allergies": [],
            "hydration_liters_per_day": None,
            "expected_version": 2,
        }
        replaced = produce(minimal, current)

        for envelope in (created, replaced):
            validator().validate(envelope)
            self.assertEqual(envelope["payload"]["version"], envelope["aggregate_version"])
            self.assertEqual(envelope["payload"]["user_id"], envelope["aggregate_id"])
            self.assertIsNotNone(envelope["causation_id"])
        self.assertEqual(created["payload"]["restrictions"], ["LACTOSE_FREE", "KOSHER"])
        self.assertEqual(created["payload"]["allergies"], ["TREE_NUTS", "SHELLFISH"])
        self.assertEqual(created["payload"]["hydration_liters_per_day"], "3.0")
        self.assertEqual(replaced["aggregate_version"], 3)
        self.assertEqual(replaced["payload"]["created_at"], "2026-10-01T08:00:00Z")

    def test_published_event_is_accepted_by_the_projector(self) -> None:
        event = parse_users_event(produce(BODY))

        assert isinstance(event, ProfileSectionUpdatedEvent)
        self.assertEqual(event.section, ProfileSection.NUTRITION_HABITS)

    def test_other_sections_and_free_text_are_rejected(self) -> None:
        """CA3: el evento no transporta otras secciones del perfil ni texto libre."""
        envelope = produce(BODY)
        for extra in (
            {"birth_date": "1992-04-18"},
            {"weight_kg": "68.5"},
            {"sports": []},
            {"notes": "kiwi"},
        ):
            with self.subTest(extra=extra):
                tampered = {**envelope, "payload": {**envelope["payload"], **extra}}
                self.assertFalse(validator().is_valid(tampered))
                with self.assertRaises(InvalidEvent):
                    parse_users_event(tampered)

    def test_numeric_hydration_is_rejected_by_schema_and_parser(self) -> None:
        envelope = produce(BODY)
        tampered = {**envelope, "payload": {**envelope["payload"], "hydration_liters_per_day": 3}}

        self.assertFalse(validator().is_valid(tampered))
        with self.assertRaises(InvalidEvent):
            parse_users_event(tampered)

    def test_catalogs_match_the_schema_and_the_openapi(self) -> None:
        payload = validator().schema["properties"]["payload"]["properties"]
        schemas = openapi()["components"]["schemas"]

        for schema_values, openapi_values, catalog in (
            (payload["diet_pattern"]["enum"], schemas["DietPattern"]["enum"], DietPattern),
            (payload["restrictions"]["items"]["enum"], schemas["Restriction"]["enum"], Restriction),
            (payload["allergies"]["items"]["enum"], schemas["Allergy"]["enum"], Allergy),
        ):
            with self.subTest(catalog=catalog.__name__):
                expected = [member.value for member in catalog]
                self.assertEqual(schema_values, expected)
                self.assertEqual(openapi_values, expected)
        self.assertEqual(payload["restrictions"]["maxItems"], len(Restriction))
        self.assertEqual(payload["allergies"]["maxItems"], len(Allergy))


class NutritionRestrictionsInternalContractTest(unittest.TestCase):
    """HU009 CA3 y CA4: el contrato interno versionado expone solo patrón, restricciones,
    alergias y versión, rechaza un token de cliente y documenta la indisponibilidad."""

    def test_response_exposes_only_the_restrictions(self) -> None:
        document = openapi()
        operation = document["paths"][INTERNAL]["get"]
        reference = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        schema = document["components"]["schemas"][reference.rsplit("/", 1)[-1]]

        self.assertEqual(
            set(schema["properties"]), {"restrictions", "allergies", "diet_pattern", "version"}
        )
        self.assertEqual(set(operation["responses"]), {"200", "403", "404", "422", "503"})

    def test_the_bff_does_not_expose_the_internal_contract(self) -> None:
        """CA4: el profesional no tiene una ruta en `bff-web` para leer hábitos alimenticios."""
        bff: dict[str, Any] = json.loads(
            (ROOT / "contracts/openapi/bff-web.openapi.json").read_text(encoding="utf-8")
        )

        self.assertFalse([path for path in bff["paths"] if "nutrition-restrictions" in path])


if __name__ == "__main__":
    unittest.main()
