"""Componente HU009 en web: BFF → Usuarios → outbox → Pub/Sub → `usuarios-read-projector` →
`athlete_profile_view.nutrition_habits` → `GET /v1/me/profile`, y el contrato interno de
restricciones que consume Entrenamientos (ADR-035).

Mismo montaje que HU005 (`ProfileFlowCase`): servicios reales con PostgreSQL de escritura y de
lectura migrados y el emulador de Pub/Sub (`TEST_DATABASE_URL`, `PUBSUB_EMULATOR_HOST`). El
bloqueo de un plan de alimentación con estos hábitos lo recorre
`test_nutrition_plan_flow.py::test_registered_habits_block_the_plan_through_the_real_contract`.
"""

import json
import unittest
import uuid
from typing import Any

from test_profile_flow import CONSENT, ProfileFlowCase

from affiliation_management.settings import FREE_PLAN_SUBSCRIPTION

RESTRICTIONS = "/v1/users/{user_id}/nutrition-restrictions"


def habits(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "consent": {"version": CONSENT},
        "diet_pattern": "VEGETARIAN",
        "restrictions": ["LOW_SUGAR", "GLUTEN_FREE"],
        "allergies": ["SESAME", "PEANUTS"],
        "meals_per_day": 4,
        "hydration_liters_per_day": "2.5",
    }
    values.update(overrides)
    return values


class NutritionHabitsFlowTest(ProfileFlowCase):
    def put_habits(self, body: Any, session: dict[str, str] | None = None) -> Any:
        session = session or self.athlete
        return self.bff.put(
            "/v1/me/nutrition-habits",
            json=body,
            headers={
                "Authorization": session["authorization"],
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )

    def restrictions(self, **headers: str) -> Any:
        """Lo que recibe Entrenamientos por el contrato interno (sin token)."""
        return self.users.get(RESTRICTIONS.format(user_id=self.athlete["user_id"]), headers=headers)

    def test_athlete_registers_habits_and_training_gets_only_the_restrictions(self) -> None:
        """CA1, CA3 y CA6 de punta a punta; Afiliación no recibe el evento (R-19)."""
        project_directory = self.publish_outbox()  # registro del deportista
        self.assertEqual(project_directory, 1)
        self.assertTrue(self.project_until(lambda: self.restrictions().status_code == 200))
        before = self.restrictions().json()

        created = self.put_habits(habits())
        replaced = self.put_habits(habits(expected_version=1, allergies=["MILK"]))
        stale = self.put_habits(habits(expected_version=1))

        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.headers["cache-control"], "no-store")
        self.assertEqual(created.json()["restrictions"], ["GLUTEN_FREE", "LOW_SUGAR"])
        self.assertEqual(created.json()["allergies"], ["PEANUTS", "SESAME"])
        self.assertEqual(created.json()["consent"]["version"], CONSENT)
        self.assertEqual((replaced.status_code, replaced.json()["version"]), (200, 2))
        self.assertEqual(
            (stale.status_code, stale.json()["details"]),
            (409, [{"field": "expected_version", "issue": "2"}]),
        )
        self.assertEqual(self.publish_outbox(), 2, "un evento por versión")

        projected = self.project_until(
            lambda: self.profile()["versions"]["nutrition_habits"] == 2  # type: ignore[no-any-return]
        )

        self.assertTrue(projected, "el proyector no aplicó los hábitos alimenticios")
        view = self.profile()
        self.assertEqual(
            view["nutrition_habits"], {k: v for k, v in replaced.json().items() if k != "user_id"}
        )
        self.assertEqual(
            view["versions"],
            {
                "demographic": None,
                "physiological": None,
                "sport_habits": None,
                "nutrition_habits": 2,
            },
        )
        self.assertEqual(
            before, {"restrictions": [], "allergies": [], "diet_pattern": None, "version": None}
        )
        internal = self.restrictions()
        self.assertEqual(internal.status_code, 200, internal.text)
        self.assertEqual(
            internal.json(),
            {
                "restrictions": ["GLUTEN_FREE", "LOW_SUGAR"],
                "allergies": ["MILK"],
                "diet_pattern": "VEGETARIAN",
                "version": 2,
            },
        )
        free_plan = self.subscriber.pull(
            request={
                "subscription": self.subscriber.subscription_path(
                    self.project, FREE_PLAN_SUBSCRIPTION
                ),
                "max_messages": 10,
            },
            timeout=30,
        ).received_messages
        self.assertEqual(
            {json.loads(item.message.data)["event_type"] for item in free_plan},
            {"usuario.registrado.v1"},
        )
        logged = self.capture.text()
        self.assertIn("audit.nutrition_restrictions_read", logged)
        for value in ("GLUTEN_FREE", "SESAME", "PEANUTS", "VEGETARIAN", "LOW_SUGAR"):
            self.assertNotIn(value, logged)

    def test_invalid_habits_other_actors_and_clients_get_nothing(self) -> None:
        """CA2, CA4 y seguridad: `422` sin estado parcial, otro actor `403`, `user_id` en el
        cuerpo `422`, el BFF no expone el contrato interno y un token de cliente se rechaza."""
        provider = self.session_for("service_provider", "profesional.hu009@example.com")

        invalid = self.put_habits(habits(allergies=["PEANUTS", "PEANUTS"], diet_pattern="PALEO"))
        no_consent = self.put_habits(habits(consent={"version": "otra-version"}))
        forbidden = self.put_habits(habits(), provider)
        with_user_id = self.put_habits(habits(user_id=self.athlete["user_id"]))
        through_bff = self.bff.get(
            RESTRICTIONS.format(user_id=self.athlete["user_id"]),
            headers={"Authorization": provider["authorization"]},
        )
        with_client_token = self.restrictions(Authorization=provider["authorization"])

        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertEqual(
            invalid.json()["details"],
            [
                {"field": "diet_pattern", "issue": "unknown_value"},
                {"field": "allergies[1]", "issue": "duplicate_value"},
            ],
        )
        self.assertEqual(
            no_consent.json()["details"],
            [{"field": "consent.version", "issue": "unsupported_consent_version"}],
        )
        self.assertEqual(
            (forbidden.status_code, forbidden.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(
            with_user_id.json()["details"], [{"field": "user_id", "issue": "field_not_allowed"}]
        )
        self.assertEqual(through_bff.status_code, 404, "bff-web no tiene esa ruta")
        self.assertEqual(
            (with_client_token.status_code, with_client_token.json()["code"]),
            (403, "permission_required"),
        )
        # Solo los registros del deportista y del profesional: ningún evento de hábitos.
        self.assertEqual(self.publish_outbox(), 2)


if __name__ == "__main__":
    unittest.main()
