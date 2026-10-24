"""BFF móvil, HU028 (ADR-041): plantillas de plan sugeridas.

Borde (`athlete` + `training.plans.suggest`), contexto interno de Usuarios (deportes y
objetivo) como filtros, `author_name` del profesional, `missing_context` y degradación a lista
no personalizada cuando Usuarios no responde. Servicios simulados con `httpx.MockTransport`.
"""

import json
import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_capabilities import FREE_CAPABILITIES, Upstream, token
from test_route_suggestions import CONTEXT, Users, error

from bff_mobile.main import create_app
from bff_mobile.settings import BffMobileSettings

SUGGESTIONS = "/v1/me/suggestions/training-plan-templates"
PROFESSIONAL_ID = str(uuid.uuid4())


def template(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "template_id": str(uuid.uuid4()),
        "professional_id": PROFESSIONAL_ID,
        "title": "Base de 10 km",
        "objectives": "Construir base aeróbica en ocho semanas.",
        "sport": "running",
        "goal": "PERFORMANCE",
        "level": "BEGINNER",
        "duration_weeks": 8,
        "weekly_sessions": [
            {"day_of_week": "MON", "description": "Rodaje suave", "duration_minutes": 40}
        ],
        "status": "PUBLISHED",
        "version": 1,
        "created_at": "2026-10-24T12:00:00Z",
        "updated_at": "2026-10-24T12:00:00Z",
        "published_at": "2026-10-24T12:00:00Z",
        "retired_at": None,
        "reasons": ["SPORT_MATCH", "GOAL_MATCH"],
    }
    values.update(overrides)
    return values


class TemplateSuggestionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.affiliation = Upstream(
            {"subscription_status": "ACTIVE", "capabilities": FREE_CAPABILITIES}
        )
        self.users = Users()
        self.users.directory = httpx.Response(
            200,
            json={"items": [{"user_id": PROFESSIONAL_ID, "full_name": "Entrenadora Sintética"}]},
        )
        self.reply: httpx.Response | type[Exception] = httpx.Response(
            200, json={"items": [template()], "personalized": True}
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

    def get(self, path: str = SUGGESTIONS, actor: str | None = "athlete", **params: str) -> Any:
        headers = {}
        if actor is not None:
            headers["Authorization"] = f"Bearer {token(actor, sub=str(self.athlete_id))}"
        return self.client.get(path, params=params, headers=headers)

    def sent_params(self) -> list[tuple[str, str]]:
        return list(self.training_requests[-1].url.params.multi_items())

    def test_sports_and_goal_reach_the_service_and_the_item_has_its_detail(self) -> None:
        """CA1 y CA4: filtros del contexto; autor, nivel, semanas, sesiones y motivo."""
        response = self.get(limit="10")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(
            self.sent_params(),
            [
                ("sport", "running"),
                ("sport", "cycling"),
                ("primary_goal", "PERFORMANCE"),
                ("limit", "10"),
            ],
        )
        body = response.json()
        [item] = body["items"]
        self.assertEqual(
            item["reference"], {"type": "training_plan_template", "id": item["template_id"]}
        )
        self.assertEqual(
            (item["title"], item["author_name"], item["level"], item["duration_weeks"]),
            ("Base de 10 km", "Entrenadora Sintética", "BEGINNER", 8),
        )
        self.assertEqual(item["summary"], "Construir base aeróbica en ocho semanas.")
        self.assertEqual(len(item["weekly_sessions"]), 1)
        self.assertEqual(item["reasons"], ["SPORT_MATCH", "GOAL_MATCH"])
        self.assertEqual((body["personalized"], body["missing_context"]), (True, []))
        self.assertEqual(body["sources"], [{"source": "users", "status": "OK"}])
        [_, lookup] = self.users.requests
        self.assertEqual(json.loads(lookup.content), {"user_ids": [PROFESSIONAL_ID]})

    def test_missing_goal_or_sports_is_reported(self) -> None:
        """CA2 y decisión del 2026-10-24: sin objetivo sigue personalizada por deporte."""
        self.users.context = httpx.Response(200, json={**CONTEXT, "primary_goal": None})
        body = self.get().json()
        self.assertEqual(self.sent_params(), [("sport", "running"), ("sport", "cycling")])
        self.assertEqual((body["personalized"], body["missing_context"]), (True, ["primary_goal"]))

        self.users.context = httpx.Response(
            200, json={**CONTEXT, "sports": [], "primary_goal": None}
        )
        # El contexto anterior quedó en caché: se consulta con otro deportista.
        self.athlete_id = uuid.uuid4()
        self.reply = httpx.Response(200, json={"items": [], "personalized": False})
        body = self.get().json()
        self.assertEqual(self.sent_params(), [])
        self.assertEqual(
            (body["personalized"], body["missing_context"]), (False, ["sports", "primary_goal"])
        )

    def test_users_down_gives_a_non_personalized_list(self) -> None:
        """CA2: Usuarios no disponible → `personalized: false` y `missing_context`."""
        self.users.context = httpx.ConnectError
        self.users.directory = httpx.ConnectError
        self.reply = httpx.Response(
            200, json={"items": [template(reasons=[])], "personalized": False}
        )

        body = self.get().json()

        self.assertEqual(self.sent_params(), [])
        self.assertEqual(
            (body["personalized"], body["missing_context"]), (False, ["sports", "primary_goal"])
        )
        self.assertEqual(body["sources"], [{"source": "users", "status": "UNAVAILABLE"}])
        self.assertIsNone(body["items"][0]["author_name"])

    def test_context_cache_is_shared_with_route_suggestions(self) -> None:
        self.get("/v1/me/suggestions/routes")
        self.get()

        calls = [path for path in self.users.paths() if path.endswith("athlete-context")]
        self.assertEqual(len(calls), 1)

    def test_edge_and_training_failures(self) -> None:
        organizer = self.get(actor="organizer")
        self.affiliation.snapshot = {"subscription_status": "ACTIVE", "capabilities": []}
        without = self.get()
        self.assertEqual(
            (organizer.status_code, organizer.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(
            (without.status_code, without.json()["code"]), (403, "capability_required")
        )
        self.assertEqual(self.training_requests, [])
        self.affiliation.snapshot = {
            "subscription_status": "ACTIVE",
            "capabilities": FREE_CAPABILITIES,
        }
        for reply, status in (
            (httpx.Response(422, json=error("validation_error")), 422),
            (httpx.Response(500, json=error("x")), 503),
            (httpx.Response(200, json={"items": [{"template_id": "x"}]}), 503),
            (httpx.ReadTimeout, 504),
        ):
            with self.subTest(status=status):
                self.reply = reply
                self.assertEqual(self.get().status_code, status)


if __name__ == "__main__":
    unittest.main()
