"""Componente HU030 (ADR-041): `bff-web` → `training-management` → outbox → Pub/Sub →
`entrenamientos-read-projector` → vista de rutinas → `bff-mobile`, con los hábitos alimenticios
reales de HU009 (`PUT /v1/me/nutrition-habits` por `bff-web` y proyección de Usuarios).

Mismo montaje que `test_route_suggestions_flow.py` (servicios reales con PostgreSQL migrado y el
emulador de Pub/Sub; se importa el módulo para no volver a descubrir sus pruebas). Los alimentos
son del catálogo vigente (`SALMON` contiene `FISH`).
"""

import os
import unittest
import uuid
from typing import Any

import test_route_suggestions_flow as routes_flow

from training_management.workers.pubsub_setup import ensure_resources

SUGGESTIONS = "/v1/me/suggestions/nutrition-routines"
BASE = "/v1/nutrition-routine-templates"
MINE = "/v1/me/nutrition-routine-templates"
# Versión de consentimiento de datos de salud de Usuarios en el montaje de HU034.
CONSENT = "health-test.v1"


def routine_body(title: str, *items: str, dietary_pattern: str = "OMNIVORE") -> dict[str, Any]:
    return {
        "title": title,
        "description": "Rutina sintética para semanas de entrenamiento.",
        "goal": "HEALTH",
        "sport": "running",
        "dietary_pattern": dietary_pattern,
        "contains_allergens": [],
        "duration_days": 14,
        "daily_meals": [
            {"meal": "LUNCH", "items": [{"food_code": code, "portions": 1} for code in items]}
        ],
    }


class NutritionRoutineSuggestionsFlowTest(unittest.TestCase):
    setUp = routes_flow.RouteSuggestionsFlowTest.setUp
    register = routes_flow.RouteSuggestionsFlowTest.register
    publish = routes_flow.RouteSuggestionsFlowTest.publish
    consume = routes_flow.RouteSuggestionsFlowTest.consume
    as_user = routes_flow.RouteSuggestionsFlowTest.as_user
    profile = routes_flow.RouteSuggestionsFlowTest.profile
    project_users = routes_flow.RouteSuggestionsFlowTest.project_users

    def publish_routine(self, professional: dict[str, str], body: dict[str, Any]) -> Any:
        response = self.web.post(
            BASE,
            json=body,
            headers=self.as_user(professional, **{"Idempotency-Key": str(uuid.uuid4())}),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def nutrition_habits(self, athlete: dict[str, str], allergies: list[str]) -> None:
        """HU009 real del deportista por el canal web."""
        response = self.web.put(
            "/v1/me/nutrition-habits",
            json={
                "consent": {"version": CONSENT},
                "diet_pattern": "OMNIVORE",
                "restrictions": [],
                "allergies": allergies,
                "meals_per_day": 4,
                "hydration_liters_per_day": "2.5",
            },
            headers=self.as_user(athlete, **{"Idempotency-Key": str(uuid.uuid4())}),
        )
        self.assertEqual(response.status_code, 201, response.text)

    def my_routines(self, professional: dict[str, str]) -> list[dict[str, Any]]:
        response = self.web.get(MINE, headers=self.as_user(professional))
        self.assertEqual(response.status_code, 200, response.text)
        items: list[dict[str, Any]] = response.json()["items"]
        return items

    @unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
    def test_compatible_routines_reach_the_athlete_and_retired_ones_do_not(self) -> None:
        """CA1 a CA4 de punta a punta con hábitos de HU009; sin HU009 → no personalizada (CA3)."""
        project_id = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        ensure_resources(project_id)
        professional, other = self.register("service_provider"), self.register("service_provider")
        allergic, sporty = self.register("athlete"), self.register("athlete")
        newcomer = self.register("athlete")
        salmon = self.publish_routine(professional, routine_body("Almuerzos", "SALMON", "SPINACH"))
        oats = self.publish_routine(professional, routine_body("Desayunos", "OATS", "BANANA"))
        retired = self.publish_routine(professional, routine_body("Antigua", "OATS"))
        foreign = self.web.post(
            f"{BASE}/{retired['template_id']}/retire",
            json={"expected_version": 1},
            headers=self.as_user(other),
        )
        retire = self.web.post(
            f"{BASE}/{retired['template_id']}/retire",
            json={"expected_version": 1},
            headers=self.as_user(professional),
        )
        # El perfil de HU006 del montaje de HU026 registra `running` con objetivo `HEALTH`.
        self.profile(allergic)
        self.profile(sporty)
        self.nutrition_habits(allergic, ["FISH"])
        self.project_users()

        self.publish(project_id)
        projected = self.consume(
            project_id,
            lambda: (
                sorted(item["status"] for item in self.my_routines(professional))
                == ["PUBLISHED", "PUBLISHED", "RETIRED"]
            ),
        )

        self.assertEqual((foreign.status_code, foreign.json()["code"]), (404, "template_not_found"))
        self.assertEqual((retire.status_code, retire.json()["status"]), (200, "RETIRED"))
        self.assertTrue(projected, "el proyector no llevó las rutinas a la vista")
        # El servicio agrega los alérgenos del catálogo de alimentos a los declarados.
        self.assertEqual(salmon["contains_allergens"], ["FISH"])
        self.assertEqual(oats["contains_allergens"], [])

        response = self.bff.get(SUGGESTIONS, headers=self.as_user(allergic))
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual([item["template_id"] for item in body["items"]], [oats["template_id"]])
        [item] = body["items"]
        self.assertEqual(
            set(item["reasons"]), {"RESTRICTION_COMPATIBLE", "GOAL_MATCH", "SPORT_MATCH"}
        )
        self.assertEqual(
            (item["author_name"], item["summary"], item["duration_days"]),
            ("Persona Sintética", "Rutina sintética para semanas de entrenamiento.", 14),
        )
        self.assertEqual((body["personalized"], body["missing_context"]), (True, []))
        self.assertEqual(body["notices"], ["NOT_MEDICAL_ADVICE"])
        self.assertEqual(body["sources"], [{"source": "users", "status": "OK"}])
        self.assertNotIn("allergies", response.text)

        without_habits = self.bff.get(SUGGESTIONS, headers=self.as_user(sporty)).json()
        self.assertEqual(
            (without_habits["personalized"], without_habits["missing_context"]),
            (False, ["nutrition_habits"]),
        )
        self.assertEqual(
            without_habits["notices"], ["RESTRICTIONS_NOT_VERIFIED", "NOT_MEDICAL_ADVICE"]
        )
        self.assertEqual(
            {item["template_id"] for item in without_habits["items"]},
            {salmon["template_id"], oats["template_id"]},
        )
        self.assertTrue(all(item["reasons"] == [] for item in without_habits["items"]))

        cold = self.bff.get(SUGGESTIONS, headers=self.as_user(newcomer)).json()
        self.assertFalse(cold["personalized"])
        self.assertIn("nutrition_habits", cold["missing_context"])
        self.assertNotIn(retired["template_id"], {item["template_id"] for item in cold["items"]})

        degraded = self.mobile_without_users.get(SUGGESTIONS, headers=self.as_user(allergic))
        self.assertEqual(degraded.status_code, 200, degraded.text)
        self.assertFalse(degraded.json()["personalized"])
        self.assertEqual(
            degraded.json()["missing_context"], ["nutrition_habits", "sports", "primary_goal"]
        )
        self.assertEqual(degraded.json()["sources"], [{"source": "users", "status": "UNAVAILABLE"}])
        self.assertIn("RESTRICTIONS_NOT_VERIFIED", degraded.json()["notices"])

    def test_validation_and_edge_through_the_web_channel(self) -> None:
        """CA4 y permiso `nutrition.plan.author`: `422` por campo; otros actores → `403`."""
        professional, organizer = self.register("service_provider"), self.register("organizer")
        athlete = self.register("athlete")

        def post(user: dict[str, str], body: dict[str, Any]) -> Any:
            return self.web.post(
                BASE,
                json=body,
                headers=self.as_user(user, **{"Idempotency-Key": str(uuid.uuid4())}),
            )

        incompatible = post(
            professional, routine_body("Vegetariana", "SALMON", dietary_pattern="VEGETARIAN")
        )
        unknown = post(professional, routine_body("Desconocida", "OATS", "NOT_A_FOOD"))
        out_of_range = post(professional, routine_body("Larga", "OATS") | {"duration_days": 91})
        forbidden = [post(user, routine_body("Ajena", "OATS")) for user in (organizer, athlete)]

        self.assertEqual(incompatible.status_code, 422, incompatible.text)
        self.assertIn(
            {
                "field": "body.daily_meals.0.items.0.food_code",
                "issue": "conflicts_with_restriction",
            },
            incompatible.json()["details"],
        )
        self.assertEqual(unknown.status_code, 422, unknown.text)
        self.assertIn(
            {"field": "body.daily_meals.0.items.1.food_code", "issue": "unknown_value"},
            unknown.json()["details"],
        )
        self.assertEqual(out_of_range.status_code, 422)
        for response in forbidden:
            self.assertEqual(
                (response.status_code, response.json()["code"]), (403, "permission_required")
            )
        self.assertEqual(self.my_routines(professional), [])


if __name__ == "__main__":
    unittest.main()
