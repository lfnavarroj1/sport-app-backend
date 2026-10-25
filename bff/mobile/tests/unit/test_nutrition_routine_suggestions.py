"""BFF móvil, HU030 (ADR-041): rutinas de alimentación sugeridas.

Borde (`athlete` + `nutrition.routines.suggest`), restricciones de HU009 (contrato interno de
Usuarios, sin caché) y contexto del deportista como filtros, `author_name` del profesional,
`missing_context`, avisos y degradación a lista no personalizada cuando no hay hábitos o
Usuarios no responde. Servicios simulados con `httpx.MockTransport`.
"""

import logging
import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_capabilities import FREE_CAPABILITIES, Upstream, token
from test_route_suggestions import CONTEXT, Users, error

from bff_mobile.main import create_app
from bff_mobile.settings import BffMobileSettings

SUGGESTIONS = "/v1/me/suggestions/nutrition-routines"
PROFESSIONAL_ID = str(uuid.uuid4())
RESTRICTIONS = {
    "restrictions": ["LOW_SUGAR"],
    "allergies": ["MILK", "FISH"],
    "diet_pattern": "PESCATARIAN",
    "version": 3,
}
WITHOUT_HABITS = {"restrictions": [], "allergies": [], "diet_pattern": None, "version": None}
NOT_PERSONALIZED = ["RESTRICTIONS_NOT_VERIFIED", "NOT_MEDICAL_ADVICE"]


def routine(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "template_id": str(uuid.uuid4()),
        "professional_id": PROFESSIONAL_ID,
        "title": "Desayunos para fondo",
        "description": "Rutina de desayunos y meriendas para semanas de fondo.",
        "goal": "PERFORMANCE",
        "sport": "running",
        "dietary_pattern": "PESCATARIAN",
        "contains_allergens": [],
        "duration_days": 14,
        "daily_meals": [
            {
                "meal": "BREAKFAST",
                "items": [
                    {"food_code": "OATS", "portions": 1},
                    {"food_code": "BANANA", "portions": 2},
                ],
            }
        ],
        "status": "PUBLISHED",
        "version": 1,
        "created_at": "2026-10-25T12:00:00Z",
        "updated_at": "2026-10-25T12:00:00Z",
        "published_at": "2026-10-25T12:00:00Z",
        "retired_at": None,
        "reasons": ["RESTRICTION_COMPATIBLE", "GOAL_MATCH", "SPORT_MATCH"],
    }
    values.update(overrides)
    return values


class NutritionUsers(Users):
    """Usuarios de HU026 más el contrato interno de restricciones alimenticias (HU009)."""

    def __init__(self) -> None:
        super().__init__()
        self.restrictions: httpx.Response | type[Exception] = httpx.Response(200, json=RESTRICTIONS)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/nutrition-restrictions"):
            return super().__call__(request)
        self.requests.append(request)
        if isinstance(self.restrictions, type):
            raise self.restrictions("usuarios")
        return self.restrictions


class NutritionRoutineSuggestionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.affiliation = Upstream(
            {"subscription_status": "ACTIVE", "capabilities": FREE_CAPABILITIES}
        )
        self.users = NutritionUsers()
        self.users.directory = httpx.Response(
            200,
            json={"items": [{"user_id": PROFESSIONAL_ID, "full_name": "Nutricionista Sintética"}]},
        )
        self.reply: httpx.Response | type[Exception] = httpx.Response(
            200, json={"items": [routine()], "personalized": True}
        )
        self.training_requests: list[httpx.Request] = []

        def training(request: httpx.Request) -> httpx.Response:
            self.training_requests.append(request)
            if isinstance(self.reply, type):
                raise self.reply("entrenamientos")
            return self.reply

        self.client = TestClient(
            create_app(
                BffMobileSettings(
                    app_env="test",  # type: ignore[arg-type]
                    users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
                    affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
                    training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
                    events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                    complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
                ),
                users_http=httpx.Client(
                    base_url="http://users", transport=httpx.MockTransport(self.users)
                ),
                affiliation_http=httpx.Client(
                    base_url="http://affiliation",
                    transport=httpx.MockTransport(self.affiliation.affiliation),
                ),
                training_http=httpx.Client(
                    base_url="http://training", transport=httpx.MockTransport(training)
                ),
                events_http=httpx.Client(
                    base_url="http://events",
                    transport=httpx.MockTransport(lambda _: httpx.Response(500)),
                ),
            )
        )
        self.athlete_id = uuid.uuid4()

    def get(self, actor: str | None = "athlete", **params: str) -> Any:
        headers = {}
        if actor is not None:
            headers["Authorization"] = f"Bearer {token(actor, sub=str(self.athlete_id))}"
        return self.client.get(SUGGESTIONS, params=params, headers=headers)

    def sent_params(self) -> list[tuple[str, str]]:
        return list(self.training_requests[-1].url.params.multi_items())

    def non_personalized(self) -> None:
        self.reply = httpx.Response(
            200, json={"items": [routine(reasons=[])], "personalized": False}
        )

    def test_restrictions_and_context_reach_the_service_and_the_item_has_its_detail(
        self,
    ) -> None:
        """CA1, CA2 y CA4: patrón, alérgenos, deportes y objetivo como filtros; detalle."""
        response = self.get(limit="10")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(
            self.sent_params(),
            [
                ("dietary_pattern", "PESCATARIAN"),
                ("exclude_allergens", "MILK"),
                ("exclude_allergens", "FISH"),
                ("sport", "running"),
                ("sport", "cycling"),
                ("primary_goal", "PERFORMANCE"),
                ("limit", "10"),
            ],
        )
        restrictions = [path for path in self.users.paths() if "nutrition-restrictions" in path]
        self.assertEqual(restrictions, [f"/v1/users/{self.athlete_id}/nutrition-restrictions"])
        body = response.json()
        [item] = body["items"]
        self.assertEqual(
            item["reference"], {"type": "nutrition_routine_template", "id": item["template_id"]}
        )
        self.assertEqual(
            (item["title"], item["author_name"], item["duration_days"], item["dietary_pattern"]),
            ("Desayunos para fondo", "Nutricionista Sintética", 14, "PESCATARIAN"),
        )
        self.assertEqual(item["summary"], "Rutina de desayunos y meriendas para semanas de fondo.")
        self.assertEqual(item["daily_meals"][0]["items"][1], {"food_code": "BANANA", "portions": 2})
        self.assertEqual(item["reasons"], ["RESTRICTION_COMPATIBLE", "GOAL_MATCH", "SPORT_MATCH"])
        self.assertEqual((body["personalized"], body["missing_context"]), (True, []))
        self.assertEqual(body["notices"], ["NOT_MEDICAL_ADVICE"])
        self.assertEqual(body["sources"], [{"source": "users", "status": "OK"}])
        self.assertTrue(body["generated_at"].endswith("Z"))
        # Las restricciones del deportista nunca se devuelven al cliente.
        self.assertNotIn("allergies", response.text)
        self.assertNotIn("LOW_SUGAR", response.text)

    def test_without_nutrition_habits_the_list_is_not_personalized(self) -> None:
        """CA3: sin HU009 (o sin cuenta resoluble) → no personalizada, `nutrition_habits`."""
        for reply in (
            httpx.Response(200, json=WITHOUT_HABITS),
            httpx.Response(404, json=error("user_not_found")),
        ):
            with self.subTest(status=reply.status_code):
                self.users.restrictions = reply
                self.non_personalized()

                body = self.get().json()

                self.assertEqual(
                    self.sent_params(),
                    [("sport", "running"), ("sport", "cycling"), ("primary_goal", "PERFORMANCE")],
                )
                self.assertEqual(
                    (body["personalized"], body["missing_context"]),
                    (False, ["nutrition_habits"]),
                )
                self.assertEqual(body["notices"], NOT_PERSONALIZED)
                self.assertEqual(len(body["items"]), 1)
                self.assertEqual(body["items"][0]["reasons"], [])
                # Un `404` también es una respuesta de Usuarios.
                self.assertEqual(body["sources"], [{"source": "users", "status": "OK"}])

    def test_restrictions_unavailable_still_delivers_the_list(self) -> None:
        """CA3: Usuarios caído o sin respuesta en restricciones → lista igual, sin verificar."""
        for reply in (
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.Response(500, json=error("x")),
            httpx.Response(200, json={"unexpected": True}),
        ):
            with self.subTest(reply=reply):
                self.users.restrictions = reply
                self.non_personalized()

                response = self.get()

                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertNotIn("dietary_pattern", dict(self.sent_params()))
                self.assertEqual(
                    (body["personalized"], body["missing_context"]),
                    (False, ["nutrition_habits"]),
                )
                self.assertEqual(body["notices"], NOT_PERSONALIZED)
                self.assertEqual(body["sources"], [{"source": "users", "status": "UNAVAILABLE"}])
                self.assertEqual(len(body["items"]), 1)

    def test_users_down_reports_every_missing_context(self) -> None:
        self.users.context = httpx.ConnectError
        self.users.restrictions = httpx.ConnectError
        self.users.directory = httpx.ConnectError
        self.non_personalized()

        body = self.get().json()

        self.assertEqual(self.sent_params(), [])
        self.assertEqual(
            (body["personalized"], body["missing_context"]),
            (False, ["nutrition_habits", "sports", "primary_goal"]),
        )
        self.assertEqual(body["notices"], NOT_PERSONALIZED)
        self.assertEqual(body["sources"], [{"source": "users", "status": "UNAVAILABLE"}])
        self.assertIsNone(body["items"][0]["author_name"])

    def test_missing_sports_or_goal_with_known_restrictions(self) -> None:
        self.users.context = httpx.Response(
            200, json={**CONTEXT, "sports": [], "primary_goal": None}
        )

        body = self.get().json()

        self.assertEqual(
            self.sent_params(),
            [
                ("dietary_pattern", "PESCATARIAN"),
                ("exclude_allergens", "MILK"),
                ("exclude_allergens", "FISH"),
            ],
        )
        self.assertEqual(body["missing_context"], ["sports", "primary_goal"])
        self.assertEqual(body["notices"], ["NOT_MEDICAL_ADVICE"])

    def test_author_name_is_null_when_the_directory_fails(self) -> None:
        self.users.directory = httpx.Response(500)

        body = self.get().json()

        self.assertIsNone(body["items"][0]["author_name"])
        self.assertTrue(body["personalized"])
        self.assertEqual(body["sources"], [{"source": "users", "status": "UNAVAILABLE"}])

    def test_restrictions_are_not_cached_but_the_context_is_shared(self) -> None:
        """Datos de salud: una consulta de restricciones por solicitud; contexto en caché."""
        self.client.get(
            "/v1/me/suggestions/training-plan-templates",
            headers={"Authorization": f"Bearer {token('athlete', sub=str(self.athlete_id))}"},
        )
        self.get()
        self.get()

        paths = self.users.paths()
        self.assertEqual(len([path for path in paths if path.endswith("athlete-context")]), 1)
        self.assertEqual(
            len([path for path in paths if path.endswith("nutrition-restrictions")]), 2
        )

    def test_health_data_never_reaches_the_logs(self) -> None:
        self.reply = httpx.Response(500, json=error("x"))
        with self.assertLogs(level=logging.DEBUG) as captured:
            logging.getLogger("sportapp.test").info("inicio")
            self.get()
            self.users.context = httpx.ConnectError
            self.users.restrictions = httpx.ReadTimeout
            self.get()

        # Mensaje y campos estructurados (`extra`) de cada registro.
        logged = "\n".join(str(record.__dict__) for record in captured.records)
        for value in ("PESCATARIAN", "FISH", "MILK", "LOW_SUGAR", "exclude_allergens"):
            self.assertNotIn(value, logged)

    def test_edge_and_training_failures(self) -> None:
        organizer = self.get(actor="organizer")
        unauthenticated = self.get(actor=None)
        self.affiliation.snapshot = {"subscription_status": "ACTIVE", "capabilities": []}
        without = self.get()
        self.assertEqual(
            (organizer.status_code, organizer.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(
            (without.status_code, without.json()["code"]), (403, "capability_required")
        )
        self.assertEqual(without.headers["Cache-Control"], "no-store")
        self.assertEqual(self.training_requests, [])
        self.assertFalse(any("nutrition-restrictions" in path for path in self.users.paths()))
        self.affiliation.snapshot = {
            "subscription_status": "ACTIVE",
            "capabilities": FREE_CAPABILITIES,
        }
        for reply, status in (
            (httpx.Response(422, json=error("validation_error")), 422),
            (httpx.Response(403, json=error("capability_required")), 403),
            (httpx.Response(503, json=error("upstream_unavailable")), 503),
            (httpx.Response(504, json=error("upstream_timeout")), 504),
            (httpx.Response(500, json=error("x")), 503),
            (httpx.Response(200, json={"items": [{"template_id": "x"}]}), 503),
            (
                httpx.Response(
                    200, json={"items": [routine(dietary_pattern="PALEO")], "personalized": True}
                ),
                503,
            ),
            (httpx.ReadTimeout, 504),
            (httpx.ConnectError, 503),
        ):
            with self.subTest(status=status, reply=reply):
                self.reply = reply
                response = self.get()
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main()
