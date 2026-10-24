"""Contrato HTTP de `PUT /v1/me/nutrition-habits` (HU009, ADR-035) con los casos de uso reales y
persistencia en memoria; el token se firma y se verifica de verdad (mismo armado que HU005)."""

import logging
import unittest
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fakes import (
    CONSENT_VERSION,
    FixedClock,
    InMemoryProfileReader,
    InMemoryProfileStore,
    nutrition_habits_body,
    update_demographic_with,
    update_nutrition_habits_with,
    update_physiological_with,
    update_sport_habits_with,
)
from fastapi.testclient import TestClient
from test_profile_api import PROFILE, VERIFIER, bearer

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from users_management.api.profile import build_profile_router
from users_management.application.ports import AthleteProfileView
from users_management.application.queries.get_profile import GetAthleteProfile

NUTRITION_HABITS = "/v1/me/nutrition-habits"
# Valores sintéticos de salud que no pueden aparecer en un UUID, un instante ni una duración.
SENSITIVE_VALUES = ("VEGETARIAN", "GLUTEN_FREE", "SESAME", "PEANUTS", "LOW_SUGAR")


class NutritionHabitsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryProfileStore()
        self.reader = InMemoryProfileReader()
        clock = FixedClock()
        app = create_service_app(
            service_name="users-management",
            title="t",
            api_version="0",
            settings=ServiceSettings(app_env="test"),  # type: ignore[call-arg]
            routers=[
                build_profile_router(
                    verify_token=VERIFIER.verify,
                    update_demographic=update_demographic_with(self.store, clock),
                    update_physiological=update_physiological_with(self.store, clock),
                    update_sport_habits=update_sport_habits_with(self.store, clock),
                    update_nutrition_habits=update_nutrition_habits_with(self.store, clock),
                    get_profile=GetAthleteProfile(self.reader),
                )
            ],
        )
        self.client = TestClient(app)
        self.user_id = uuid4()
        self.auth = bearer(self.user_id)

    def habits(self, body: Any, key: str | None = "new", auth: dict[str, str] | None = None) -> Any:
        headers = dict(self.auth if auth is None else auth)
        if key == "new":
            key = str(uuid4())
        if key is not None:
            headers["Idempotency-Key"] = key
        return self.client.put(NUTRITION_HABITS, json=body, headers=headers)

    def details(self, response: Any) -> list[tuple[str | None, str]]:
        return [(item["field"], item["issue"]) for item in response.json()["details"]]

    # --- CA1: alta, reemplazo y normalización ---

    def test_nutrition_habits_201_then_200_normalized(self) -> None:
        """CA1: `201` al crear y `200` al reemplazar, con el estado normalizado y su versión."""
        created = self.habits(nutrition_habits_body())

        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.headers["Cache-Control"], "no-store")
        self.assertEqual(
            created.json(),
            {
                "user_id": str(self.user_id),
                "version": 1,
                "consent": {"version": CONSENT_VERSION, "accepted_at": "2026-10-07T12:00:00Z"},
                "diet_pattern": "VEGETARIAN",
                "restrictions": ["GLUTEN_FREE", "LOW_SUGAR"],
                "allergies": ["PEANUTS", "SESAME"],
                "meals_per_day": 4,
                "hydration_liters_per_day": "2.5",
                "created_at": "2026-10-07T12:00:00Z",
                "updated_at": "2026-10-07T12:00:00Z",
            },
        )

        replaced = self.habits(
            nutrition_habits_body(
                expected_version=1,
                diet_pattern="VEGAN",
                restrictions=[],
                allergies=[],
                hydration_liters_per_day=None,
            )
        )

        self.assertEqual(replaced.status_code, 200, replaced.text)
        body = replaced.json()
        self.assertEqual(
            (body["version"], body["diet_pattern"], body["restrictions"], body["allergies"]),
            (2, "VEGAN", [], []),
        )
        self.assertIsNone(body["hydration_liters_per_day"])
        self.assertEqual(body["created_at"], "2026-10-07T12:00:00Z")

    def test_successful_command_publishes_the_complete_section_once(self) -> None:
        """CA3: un evento con el estado completo y sin otras secciones del perfil."""
        created = self.habits(nutrition_habits_body()).json()

        [message] = self.store.outbox
        self.assertEqual(
            (message.event_type, message.aggregate_type, message.aggregate_id),
            ("habitos.alimenticios_actualizados.v1", "nutrition_habits", self.user_id),
        )
        self.assertEqual((message.aggregate_version, message.schema_version), (1, 1))
        self.assertEqual(message.payload, created)
        self.assertFalse({"birth_date", "weight_kg", "sports"} & set(message.payload))

    def test_profile_query_returns_the_projected_habits(self) -> None:
        """CA1: la consulta del perfil refleja la sección una vez proyectada."""
        section = {
            k: v for k, v in self.habits(nutrition_habits_body()).json().items() if k != "user_id"
        }
        self.reader.views[self.user_id] = AthleteProfileView(
            user_id=self.user_id,
            demographic=None,
            physiological=None,
            sport_habits=None,
            nutrition_habits=section,
            demographic_version=None,
            physiological_version=None,
            sport_habits_version=None,
            nutrition_habits_version=1,
            projected_at=datetime(2026, 10, 7, 12, 0, 1, tzinfo=UTC),
        )

        body = self.client.get(PROFILE, headers=self.auth).json()

        self.assertEqual(body["nutrition_habits"], section)
        self.assertEqual(body["versions"]["nutrition_habits"], 1)

    # --- CA2: validación todo o nada ---

    def test_invalid_body_keeps_the_previous_state(self) -> None:
        """CA2: `422 validation_error` con `details` y sin nueva versión ni evento."""
        self.habits(nutrition_habits_body())

        response = self.habits(
            nutrition_habits_body(
                expected_version=1,
                allergies=["PEANUTS", "PEANUTS"],
                restrictions=["SIN_AZUCAR"],
                hydration_liters_per_day="2.55",
            )
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "validation_error")
        self.assertEqual(
            self.details(response),
            [
                ("restrictions[0]", "unknown_value"),
                ("allergies[1]", "duplicate_value"),
                ("hydration_liters_per_day", "too_many_decimals"),
            ],
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(len(self.store.outbox), 1)

    def test_consent_missing_or_outdated(self) -> None:
        """CA2: consentimiento ausente o distinto de la versión vigente."""
        body = nutrition_habits_body()
        del body["consent"]
        missing = self.habits(body)
        outdated = self.habits(nutrition_habits_body(consent={"version": "health-2025.v0"}))

        self.assertEqual(self.details(missing), [("consent.version", "required")])
        self.assertEqual(
            self.details(outdated), [("consent.version", "unsupported_consent_version")]
        )
        self.assertEqual(self.store.outbox, [])

    def test_json_types_and_free_text_are_rejected(self) -> None:
        """CA2: un número donde va la cadena decimal, listas que no son listas y texto libre."""
        response = self.habits(
            nutrition_habits_body(
                hydration_liters_per_day=2.5,
                restrictions="GLUTEN_FREE",
                allergies=["PEANUTS", 3],
                notes="alergia al kiwi",
            )
        )

        self.assertEqual(
            sorted(self.details(response), key=str),
            sorted(
                [
                    ("notes", "field_not_allowed"),
                    ("restrictions", "unknown_value"),
                    ("allergies[1]", "unknown_value"),
                    ("hydration_liters_per_day", "unknown_value"),
                ],
                key=str,
            ),
        )
        self.assertEqual(self.store.outbox, [])

    def test_user_id_in_the_body_is_rejected(self) -> None:
        """Seguridad: el deportista sale del token; `user_id` en el cuerpo → `422`."""
        response = self.habits(nutrition_habits_body(user_id=str(uuid4())))

        self.assertEqual(self.details(response), [("user_id", "field_not_allowed")])
        self.assertEqual(self.store.outbox, [])

    def test_key_problems_are_reported_with_body_violations(self) -> None:
        missing = self.habits(nutrition_habits_body(meals_per_day=0), key=None)
        invalid = self.habits(nutrition_habits_body(), key="no-es-uuid")

        self.assertEqual(
            self.details(missing),
            [("Idempotency-Key", "required"), ("meals_per_day", "out_of_range")],
        )
        self.assertEqual(self.details(invalid), [("Idempotency-Key", "unknown_value")])

    # --- CA6: idempotencia y versión ---

    def test_retry_with_same_key_returns_the_original(self) -> None:
        """CA6: la misma clave con la misma información (en otro orden) → la respuesta original."""
        key = str(uuid4())
        first = self.habits(nutrition_habits_body(), key=key)
        reordered = nutrition_habits_body(
            restrictions=["GLUTEN_FREE", "LOW_SUGAR"], allergies=["PEANUTS", "SESAME"]
        )

        retry = self.habits(reordered, key=key)

        self.assertEqual((retry.status_code, retry.json()), (201, first.json()))
        self.assertEqual(retry.headers["Idempotent-Replayed"], "true")
        self.assertEqual(len(self.store.outbox), 1)

    def test_key_reused_with_other_payload_or_athlete(self) -> None:
        key = str(uuid4())
        self.habits(nutrition_habits_body(), key=key)

        other_payload = self.habits(nutrition_habits_body(meals_per_day=5), key=key)
        other_athlete = self.habits(nutrition_habits_body(), key=key, auth=bearer(uuid4()))

        for response in (other_payload, other_athlete):
            self.assertEqual(
                (response.status_code, response.json()["code"]), (409, "idempotency_key_reused")
            )
        self.assertEqual(len(self.store.outbox), 1)

    def test_stale_or_missing_expected_version_is_a_conflict(self) -> None:
        """CA6: `409 version_conflict` con la versión vigente (`0` si no existe)."""
        premature = self.habits(nutrition_habits_body(expected_version=1))
        self.habits(nutrition_habits_body())
        missing = self.habits(nutrition_habits_body(meals_per_day=3))
        stale = self.habits(nutrition_habits_body(expected_version=3))

        for response, issue in ((premature, "0"), (missing, "1"), (stale, "1")):
            self.assertEqual(response.json()["code"], "version_conflict")
            self.assertEqual(self.details(response), [("expected_version", issue)])
        self.assertEqual(len(self.store.outbox), 1)

    # --- CA4 y seguridad: autorización ---

    def test_only_athletes_register_their_own_habits(self) -> None:
        """Otros actores → `403 permission_required` (un profesional no registra ni lee hábitos
        ajenos por esta API); sin token → `401`."""
        for actor in ("organizer", "service_provider"):
            with self.subTest(actor=actor):
                command = self.habits(nutrition_habits_body(), auth=bearer(uuid4(), actor))
                query = self.client.get(PROFILE, headers=bearer(uuid4(), actor))
                for response in (command, query):
                    self.assertEqual(
                        (response.status_code, response.json()["code"]),
                        (403, "permission_required"),
                    )
        self.assertEqual(self.habits(nutrition_habits_body(), auth={}).status_code, 401)
        self.assertEqual(self.store.outbox, [])

    def test_logs_and_audit_never_contain_habits(self) -> None:
        """Minimización: ni logs ni auditoría contienen alergias, restricciones ni patrón."""
        with self.assertLogs(logging.getLogger(), level="DEBUG") as logs:
            self.habits(nutrition_habits_body())
            self.habits(nutrition_habits_body(diet_pattern="CARNIVORE"))

        rendered = " ".join(logs.output) + " ".join(
            str(getattr(record, "fields", "")) for record in logs.records
        )
        for value in (*SENSITIVE_VALUES, "CARNIVORE"):
            self.assertNotIn(value, rendered)
        audit = [
            record.fields  # type: ignore[attr-defined]
            for record in logs.records
            if record.name == "sportapp.audit"
        ]
        self.assertEqual(
            [(entry["section"], entry["result"]) for entry in audit],
            [("nutrition_habits", "created"), ("nutrition_habits", "rejected")],
        )


if __name__ == "__main__":
    unittest.main()
