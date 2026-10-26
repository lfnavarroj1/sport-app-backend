"""BFF web, HU028 (ADR-041): filtro de borde (`service_provider` con `training.plan.author`) y
reenvío de la publicación, el retiro y la lista de plantillas a un `training-management`
simulado con `httpx.MockTransport`. La aplicación se crea una vez por clase."""

import json
import unittest
import uuid
from typing import Any, ClassVar

import httpx
from fastapi.testclient import TestClient
from test_training import Upstreams, token

from bff_web.main import create_app
from bff_web.settings import BffWebSettings

BASE = "/v1/training-plan-templates"
TEMPLATE_ID = uuid.uuid4()


def template_request(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": "Base de 10 km",
        "objectives": "Construir base aeróbica en ocho semanas.",
        "sport": "running",
        "goal": "PERFORMANCE",
        "level": "BEGINNER",
        "duration_weeks": 8,
        "weekly_sessions": [
            {"day_of_week": "MON", "description": "Rodaje suave", "duration_minutes": 40}
        ],
    }
    body.update(overrides)
    return body


def template(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        **template_request(),
        "template_id": str(TEMPLATE_ID),
        "professional_id": str(uuid.uuid4()),
        "status": "PUBLISHED",
        "version": 1,
        "created_at": "2026-10-24T12:00:00Z",
        "updated_at": "2026-10-24T12:00:00Z",
        "published_at": "2026-10-24T12:00:00Z",
        "retired_at": None,
    }
    values.update(overrides)
    return values


def error(code: str) -> dict[str, Any]:
    return {"code": code, "message": "m", "correlation_id": None, "details": []}


class TrainingPlanTemplatesTest(unittest.TestCase):
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
        self.upstreams.training_response = httpx.Response(201, json=template())
        self.upstreams.training_down = None

    def auth(self, actor: str | None = "service_provider", **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(actor)}", **extra}

    def publish(
        self, body: dict[str, Any] | None = None, actor: str | None = "service_provider"
    ) -> Any:
        return self.client.post(
            BASE,
            json=body or template_request(),
            headers=self.auth(actor, **{"Idempotency-Key": str(uuid.uuid4())}),
        )

    def test_publish_forwards_body_key_and_token_and_relays_headers(self) -> None:
        """CA4: cuerpo, clave y token llegan al servicio; `201` sin alterar."""
        self.upstreams.training_response = httpx.Response(
            201,
            json=template(),
            headers={"Location": f"{BASE}/{TEMPLATE_ID}", "Idempotent-Replayed": "true"},
        )
        key, raw = str(uuid.uuid4()), token("service_provider")

        response = self.client.post(
            BASE,
            json=template_request(),
            headers={"Authorization": f"Bearer {raw}", "Idempotency-Key": key},
        )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["template_id"], str(TEMPLATE_ID))
        self.assertEqual(response.headers["Location"], f"{BASE}/{TEMPLATE_ID}")
        self.assertEqual(response.headers["Idempotent-Replayed"], "true")
        [forwarded] = self.upstreams.training_requests
        self.assertEqual((forwarded.method, forwarded.url.path), ("POST", BASE))
        self.assertEqual(
            (forwarded.headers["Idempotency-Key"], forwarded.headers["Authorization"]),
            (key, f"Bearer {raw}"),
        )
        self.assertEqual(json.loads(forwarded.content), template_request())

    def test_only_plan_authors_reach_the_service(self) -> None:
        for actor in ("athlete", "organizer", None):
            with self.subTest(actor=actor):
                for response in (
                    self.publish(actor=actor),
                    self.client.post(
                        f"{BASE}/{TEMPLATE_ID}/retire",
                        json={"expected_version": 1},
                        headers=self.auth(actor),
                    ),
                    self.client.get("/v1/me/training-plan-templates", headers=self.auth(actor)),
                ):
                    self.assertEqual(
                        (response.status_code, response.json()["code"]),
                        (403, "permission_required"),
                    )
        self.assertEqual(self.client.get("/v1/me/training-plan-templates").status_code, 401)
        self.assertEqual(self.upstreams.training_requests, [])

    def test_shape_errors_are_rejected_at_the_edge(self) -> None:
        """CA4: la forma del cuerpo se valida como en el servicio; no se reenvía."""
        for body in (
            template_request(duration_weeks=53),
            template_request(weekly_sessions=[]),
            template_request(athlete_id=str(uuid.uuid4())),
        ):
            with self.subTest(body=body):
                self.assertEqual(self.publish(body).status_code, 422)
        self.assertEqual(self.upstreams.training_requests, [])

    def test_service_errors_and_queries_are_relayed_unchanged(self) -> None:
        """CA3: `404 template_not_found` y `409` del servicio sin alterar; lista con `no-store`."""
        for status, body in ((404, error("template_not_found")), (409, error("version_conflict"))):
            with self.subTest(status=status):
                self.upstreams.training_response = httpx.Response(status, json=body)
                response = self.client.post(
                    f"{BASE}/{TEMPLATE_ID}/retire",
                    json={"expected_version": 1},
                    headers=self.auth(),
                )
                self.assertEqual((response.status_code, response.json()), (status, body))
        self.assertEqual(
            json.loads(self.upstreams.training_requests[-1].content), {"expected_version": 1}
        )
        page = {"items": [template()], "next_cursor": None}
        self.upstreams.training_response = httpx.Response(200, json=page)
        listed = self.client.get(
            "/v1/me/training-plan-templates", params={"limit": "5"}, headers=self.auth()
        )
        self.assertEqual((listed.status_code, listed.json()), (200, page))
        self.assertEqual(listed.headers["Cache-Control"], "no-store")
        self.assertEqual(dict(self.upstreams.training_requests[-1].url.params), {"limit": "5"})

    def test_training_down_or_unexpected_is_503_or_504(self) -> None:
        self.upstreams.training_down = httpx.ConnectTimeout
        self.assertEqual(self.publish().status_code, 504)
        self.upstreams.training_down = None
        self.upstreams.training_response = httpx.Response(500, json=error("x"))
        self.assertEqual(self.publish().status_code, 503)


if __name__ == "__main__":
    unittest.main()
