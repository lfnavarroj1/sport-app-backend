"""BFF web, HU030 (ADR-041): filtro de borde (`service_provider` con `nutrition.plan.author`) y
reenvío de la publicación, el retiro y la lista de rutinas de alimentación a un
`training-management` simulado con `httpx.MockTransport`. La aplicación se crea una vez por
clase."""

import json
import unittest
import uuid
from typing import Any, ClassVar

import httpx
from fastapi.testclient import TestClient
from test_training import Upstreams, token

from bff_web.main import create_app
from bff_web.settings import BffWebSettings

BASE = "/v1/nutrition-routine-templates"
MINE = "/v1/me/nutrition-routine-templates"
TEMPLATE_ID = uuid.uuid4()


def routine_request(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": "Desayunos para fondo",
        "description": "Rutina de desayunos y meriendas para semanas de fondo.",
        "goal": "PERFORMANCE",
        "sport": "running",
        "dietary_pattern": "OMNIVORE",
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
    }
    body.update(overrides)
    return body


def routine(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        **routine_request(),
        "template_id": str(TEMPLATE_ID),
        "professional_id": str(uuid.uuid4()),
        "status": "PUBLISHED",
        "version": 1,
        "created_at": "2026-10-25T12:00:00Z",
        "updated_at": "2026-10-25T12:00:00Z",
        "published_at": "2026-10-25T12:00:00Z",
        "retired_at": None,
    }
    values.update(overrides)
    return values


def error(code: str, details: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {"code": code, "message": "m", "correlation_id": None, "details": details or []}


class NutritionRoutineTemplatesTest(unittest.TestCase):
    client: ClassVar[TestClient]
    upstreams: ClassVar[Upstreams]

    @classmethod
    def setUpClass(cls) -> None:
        cls.upstreams = Upstreams()
        settings = BffWebSettings(
            app_env="test",  # type: ignore[arg-type]
            users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
            affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
            training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
            complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
            events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
        )
        cls.client = TestClient(
            create_app(
                settings,
                users_http=httpx.Client(
                    transport=httpx.MockTransport(cls.upstreams.users), base_url="http://users"
                ),
                training_http=httpx.Client(
                    transport=httpx.MockTransport(cls.upstreams.training),
                    base_url="http://training",
                ),
            )
        )

    def setUp(self) -> None:
        self.upstreams.training_requests.clear()
        self.upstreams.training_response = httpx.Response(201, json=routine())
        self.upstreams.training_down = None

    def auth(self, actor: str | None = "service_provider", **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(actor)}", **extra}

    def publish(
        self, body: dict[str, Any] | None = None, actor: str | None = "service_provider"
    ) -> Any:
        return self.client.post(
            BASE,
            json=body or routine_request(),
            headers=self.auth(actor, **{"Idempotency-Key": str(uuid.uuid4())}),
        )

    def retire(self, actor: str | None = "service_provider") -> Any:
        return self.client.post(
            f"{BASE}/{TEMPLATE_ID}/retire", json={"expected_version": 1}, headers=self.auth(actor)
        )

    def test_publish_forwards_body_key_and_token_and_relays_headers(self) -> None:
        """Cuerpo, clave y token llegan al servicio; `201` y cabeceras sin alterar."""
        self.upstreams.training_response = httpx.Response(
            201,
            json=routine(contains_allergens=["FISH"]),
            headers={"Location": f"{BASE}/{TEMPLATE_ID}", "Idempotent-Replayed": "true"},
        )
        key, raw = str(uuid.uuid4()), token("service_provider")

        response = self.client.post(
            BASE,
            json=routine_request(),
            headers={"Authorization": f"Bearer {raw}", "Idempotency-Key": key},
        )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["template_id"], str(TEMPLATE_ID))
        self.assertEqual(response.json()["contains_allergens"], ["FISH"])
        self.assertEqual(response.headers["Location"], f"{BASE}/{TEMPLATE_ID}")
        self.assertEqual(response.headers["Idempotent-Replayed"], "true")
        [forwarded] = self.upstreams.training_requests
        self.assertEqual((forwarded.method, forwarded.url.path), ("POST", BASE))
        self.assertEqual(
            (forwarded.headers["Idempotency-Key"], forwarded.headers["Authorization"]),
            (key, f"Bearer {raw}"),
        )
        self.assertEqual(json.loads(forwarded.content), routine_request())

    def test_sport_is_null_by_default(self) -> None:
        body = routine_request()
        del body["sport"]

        self.assertEqual(self.publish(body).status_code, 201)
        self.assertIsNone(json.loads(self.upstreams.training_requests[-1].content)["sport"])

    def test_only_nutrition_authors_reach_the_service(self) -> None:
        for actor in ("athlete", "organizer", None):
            with self.subTest(actor=actor):
                for response in (
                    self.publish(actor=actor),
                    self.retire(actor),
                    self.client.get(MINE, headers=self.auth(actor)),
                ):
                    self.assertEqual(
                        (response.status_code, response.json()["code"]),
                        (403, "permission_required"),
                    )
        self.assertEqual(self.client.get(MINE).status_code, 401)
        invalid = self.client.post(
            BASE,
            json=routine_request(),
            headers={"Authorization": "Bearer x", "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(
            (invalid.status_code, invalid.json()["code"]), (401, "invalid_access_token")
        )
        self.assertEqual(self.upstreams.training_requests, [])

    def test_shape_errors_are_rejected_at_the_edge(self) -> None:
        """La forma del cuerpo se valida como en el servicio; no se reenvía."""
        meal = {"meal": "LUNCH", "items": [{"food_code": "OATS", "portions": 1}]}
        for body in (
            routine_request(duration_days=91),
            routine_request(daily_meals=[]),
            routine_request(daily_meals=[meal] * 9),
            routine_request(daily_meals=[{"meal": "LUNCH", "items": []}]),
            routine_request(
                daily_meals=[{"meal": "LUNCH", "items": [{"food_code": "OATS", "portions": 11}]}]
            ),
            routine_request(contains_allergens=["GLUTEN"]),
            routine_request(dietary_pattern="PALEO"),
            routine_request(athlete_id=str(uuid.uuid4())),
        ):
            with self.subTest(body=body):
                self.assertEqual(self.publish(body).status_code, 422)
        missing_key = self.client.post(BASE, json=routine_request(), headers=self.auth())
        self.assertEqual(missing_key.status_code, 422)
        self.assertEqual(self.upstreams.training_requests, [])

    def test_service_rule_errors_are_relayed_unchanged(self) -> None:
        """`422` del catálogo y `409` de idempotencia del servicio sin alterar."""
        for status, body in (
            (
                422,
                error(
                    "validation_error",
                    [
                        {
                            "field": "body.daily_meals.0.items.1.food_code",
                            "issue": "conflicts_with_restriction",
                        }
                    ],
                ),
            ),
            (409, error("idempotency_key_reused")),
            (403, error("permission_required")),
        ):
            with self.subTest(status=status):
                self.upstreams.training_response = httpx.Response(status, json=body)
                response = self.publish()
                self.assertEqual((response.status_code, response.json()), (status, body))

    def test_retire_and_list_are_relayed_unchanged(self) -> None:
        """`404 template_not_found` y `409 version_conflict` sin alterar; lista con `no-store`."""
        self.upstreams.training_response = httpx.Response(
            200, json=routine(status="RETIRED", version=2)
        )
        retired = self.retire()
        self.assertEqual((retired.status_code, retired.json()["status"]), (200, "RETIRED"))
        forwarded = self.upstreams.training_requests[-1]
        self.assertEqual(
            (forwarded.method, forwarded.url.path), ("POST", f"{BASE}/{TEMPLATE_ID}/retire")
        )
        self.assertEqual(json.loads(forwarded.content), {"expected_version": 1})
        for status, body in (
            (404, error("template_not_found")),
            (409, error("version_conflict", [{"field": "expected_version", "issue": "2"}])),
        ):
            with self.subTest(status=status):
                self.upstreams.training_response = httpx.Response(status, json=body)
                response = self.retire()
                self.assertEqual((response.status_code, response.json()), (status, body))
        self.assertEqual(
            self.client.post(
                f"{BASE}/{TEMPLATE_ID}/retire", json={"expected_version": 0}, headers=self.auth()
            ).status_code,
            422,
        )

        page = {"items": [routine()], "next_cursor": "c2"}
        self.upstreams.training_response = httpx.Response(200, json=page)
        listed = self.client.get(MINE, params={"limit": "5", "cursor": "c1"}, headers=self.auth())
        self.assertEqual((listed.status_code, listed.json()), (200, page))
        self.assertEqual(listed.headers["Cache-Control"], "no-store")
        self.assertEqual(
            dict(self.upstreams.training_requests[-1].url.params), {"limit": "5", "cursor": "c1"}
        )
        self.upstreams.training_response = httpx.Response(
            422,
            json=error("validation_error", [{"field": "query.cursor", "issue": "invalid_cursor"}]),
        )
        invalid = self.client.get(MINE, params={"cursor": "x"}, headers=self.auth())
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.headers["Cache-Control"], "no-store")

    def test_training_down_or_unexpected_is_503_or_504(self) -> None:
        self.upstreams.training_down = httpx.ConnectTimeout
        self.assertEqual(self.publish().status_code, 504)
        self.assertEqual(self.retire().status_code, 504)
        listed = self.client.get(MINE, headers=self.auth())
        self.assertEqual(listed.status_code, 504)
        self.assertEqual(listed.headers["Cache-Control"], "no-store")
        self.upstreams.training_down = httpx.ConnectError
        self.assertEqual(self.publish().status_code, 503)
        self.upstreams.training_down = None
        self.upstreams.training_response = httpx.Response(500, json=error("x"))
        self.assertEqual(self.publish().status_code, 503)
        self.assertEqual(self.client.get(MINE, headers=self.auth()).status_code, 503)


if __name__ == "__main__":
    unittest.main()
