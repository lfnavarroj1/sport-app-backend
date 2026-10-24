"""Componente HU028 (ADR-041): `bff-web` → `training-management` → outbox → Pub/Sub →
`entrenamientos-read-projector` → `training_plan_template_view` → `bff-mobile`.

Mismo montaje que `test_route_suggestions_flow.py` (servicios reales con PostgreSQL migrado y el
emulador de Pub/Sub; se importa el módulo para no volver a descubrir sus pruebas).
"""

import os
import unittest
import uuid
from typing import Any

import test_route_suggestions_flow as routes_flow
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from training_management.infrastructure.persistence_read.models import TrainingPlanTemplateViewRow
from training_management.workers.pubsub_setup import ensure_resources

SUGGESTIONS = "/v1/me/suggestions/training-plan-templates"
BASE = "/v1/training-plan-templates"


def template_body(title: str, sport: str, goal: str) -> dict[str, Any]:
    return {
        "title": title,
        "objectives": "Construir base aeróbica en ocho semanas.",
        "sport": sport,
        "goal": goal,
        "level": "BEGINNER",
        "duration_weeks": 8,
        "weekly_sessions": [
            {"day_of_week": "MON", "description": "Rodaje suave", "duration_minutes": 40},
            {"day_of_week": "SAT", "description": "Fondo", "duration_minutes": 90},
        ],
    }


class TemplateSuggestionsFlowTest(unittest.TestCase):
    setUp = routes_flow.RouteSuggestionsFlowTest.setUp
    register = routes_flow.RouteSuggestionsFlowTest.register
    publish = routes_flow.RouteSuggestionsFlowTest.publish
    consume = routes_flow.RouteSuggestionsFlowTest.consume
    as_user = routes_flow.RouteSuggestionsFlowTest.as_user
    profile = routes_flow.RouteSuggestionsFlowTest.profile
    project_users = routes_flow.RouteSuggestionsFlowTest.project_users

    def publish_template(self, professional: dict[str, str], body: dict[str, Any]) -> Any:
        response = self.web.post(
            BASE,
            json=body,
            headers=self.as_user(professional, **{"Idempotency-Key": str(uuid.uuid4())}),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def views(self, status: str | None = None) -> int:
        statement = select(func.count()).select_from(TrainingPlanTemplateViewRow)
        if status is not None:
            statement = statement.where(TrainingPlanTemplateViewRow.status == status)
        with sessionmaker(self.engines["training_r"])() as session:
            return session.scalar(statement) or 0

    @unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
    def test_published_templates_reach_the_athlete_and_retired_ones_do_not(self) -> None:
        """CA1 a CA4 de punta a punta; Usuarios caído → no personalizada (CA2)."""
        project_id = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        ensure_resources(project_id)
        professional, other = self.register("service_provider"), self.register("service_provider")
        athlete, newcomer = self.register("athlete"), self.register("athlete")
        health = self.publish_template(professional, template_body("Salud", "running", "HEALTH"))
        goal = self.publish_template(professional, template_body("Meta", "running", "RECREATION"))
        bike = self.publish_template(professional, template_body("Bici", "cycling", "HEALTH"))
        retired = self.publish_template(professional, template_body("Vieja", "running", "HEALTH"))
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
        self.profile(athlete)
        self.project_users()

        self.publish(project_id)
        projected = self.consume(
            project_id, lambda: self.views() == 4 and self.views("RETIRED") == 1
        )

        self.assertEqual((foreign.status_code, foreign.json()["code"]), (404, "template_not_found"))
        self.assertEqual((retire.status_code, retire.json()["status"]), (200, "RETIRED"))
        self.assertTrue(projected, "el proyector no llevó las plantillas a la vista")
        mine = self.web.get("/v1/me/training-plan-templates", headers=self.as_user(professional))
        self.assertEqual(len(mine.json()["items"]), 4)

        body = self.bff.get(SUGGESTIONS, headers=self.as_user(athlete)).json()
        self.assertEqual(
            [(item["template_id"], item["reasons"]) for item in body["items"]],
            [
                (health["template_id"], ["SPORT_MATCH", "GOAL_MATCH"]),
                (goal["template_id"], ["SPORT_MATCH"]),
            ],
        )
        [first, _] = body["items"]
        self.assertEqual(
            (first["author_name"], first["level"], first["duration_weeks"]),
            ("Persona Sintética", "BEGINNER", 8),
        )
        self.assertEqual(len(first["weekly_sessions"]), 2)
        self.assertEqual((body["personalized"], body["missing_context"]), (True, []))

        cold = self.bff.get(SUGGESTIONS, headers=self.as_user(newcomer)).json()
        self.assertEqual(
            (cold["personalized"], cold["missing_context"]), (False, ["sports", "primary_goal"])
        )
        self.assertEqual(
            [item["template_id"] for item in cold["items"]],
            [bike["template_id"], goal["template_id"], health["template_id"]],
        )
        degraded = self.mobile_without_users.get(SUGGESTIONS, headers=self.as_user(athlete))
        self.assertEqual(degraded.status_code, 200, degraded.text)
        self.assertFalse(degraded.json()["personalized"])
        self.assertEqual(degraded.json()["sources"], [{"source": "users", "status": "UNAVAILABLE"}])

    def test_validation_and_edge_through_the_web_channel(self) -> None:
        """CA4 y permiso `training.plan.author`: `422` por campo; otros actores → `403`."""
        professional, organizer = self.register("service_provider"), self.register("organizer")

        invalid = self.web.post(
            BASE,
            json=template_body("Salud", "running", "HEALTH") | {"duration_weeks": 0},
            headers=self.as_user(professional, **{"Idempotency-Key": str(uuid.uuid4())}),
        )
        forbidden = self.web.post(
            BASE,
            json=template_body("Salud", "running", "HEALTH"),
            headers=self.as_user(organizer, **{"Idempotency-Key": str(uuid.uuid4())}),
        )

        self.assertEqual(invalid.status_code, 422)
        self.assertIn(
            {"field": "body.duration_weeks", "issue": "greater_than_equal"},
            invalid.json()["details"],
        )
        self.assertEqual(
            (forbidden.status_code, forbidden.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(self.views(), 0)


if __name__ == "__main__":
    unittest.main()
