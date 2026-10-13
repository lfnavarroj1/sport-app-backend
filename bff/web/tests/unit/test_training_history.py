"""BFF web, HU011: filtro de borde, reenvío de filtros y errores del historial."""

import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_training import Upstreams, token

from bff_web.main import create_app
from bff_web.settings import BffWebSettings

PAGE = {"items": [], "next_cursor": None, "filters": {"sports": ["running"]}}


class TrainingHistoryRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstreams = Upstreams()
        self.upstreams.training_response = httpx.Response(200, json=PAGE)
        self.client = TestClient(
            create_app(
                BffWebSettings(
                    app_env="test",  # type: ignore[arg-type]
                    users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
                    affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
                    training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
                    complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
                ),
                users_http=httpx.Client(
                    transport=httpx.MockTransport(self.upstreams.users), base_url="http://users"
                ),
                affiliation_http=httpx.Client(
                    transport=httpx.MockTransport(lambda _: httpx.Response(500)),
                    base_url="http://affiliation",
                ),
                training_http=httpx.Client(
                    transport=httpx.MockTransport(self.upstreams.training),
                    base_url="http://training",
                ),
            )
        )

    def get(self, path: str, actor: str | None, **params: Any) -> Any:
        headers = {"Authorization": f"Bearer {token(actor)}"} if actor else {}
        return self.client.get(path, headers=headers, params=params)

    def test_own_history_forwards_filters_and_token(self) -> None:
        response = self.get(
            "/v1/training-history",
            "athlete",
            **{
                "from": "2026-10-01",
                "to": "2026-10-31",
                "sport": "running",
                "limit": 5,
                "cursor": "abc",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), PAGE)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        [forwarded] = self.upstreams.training_requests
        self.assertEqual(forwarded.url.path, "/v1/training-history")
        self.assertEqual(
            dict(forwarded.url.params),
            {
                "from": "2026-10-01",
                "to": "2026-10-31",
                "sport": "running",
                "limit": "5",
                "cursor": "abc",
            },
        )
        self.assertTrue(forwarded.headers["Authorization"].startswith("Bearer "))

    def test_default_limit_and_athlete_route(self) -> None:
        athlete_id = uuid.uuid4()

        response = self.get(f"/v1/athletes/{athlete_id}/training-history", "service_provider")

        self.assertEqual(response.status_code, 200)
        [forwarded] = self.upstreams.training_requests
        self.assertEqual(forwarded.url.path, f"/v1/athletes/{athlete_id}/training-history")
        self.assertEqual(dict(forwarded.url.params), {"limit": "20"})

    def test_edge_filters_by_actor_and_permission(self) -> None:
        """Defensa en el borde; la autorización vigente la decide el servicio (CA3)."""
        athlete_path = f"/v1/athletes/{uuid.uuid4()}/training-history"
        cases = (
            ("/v1/training-history", "service_provider"),
            ("/v1/training-history", "organizer"),
            (athlete_path, "athlete"),
            (athlete_path, "organizer"),
            (athlete_path, None),
        )
        for path, actor in cases:
            with self.subTest(path=path[:12], actor=actor):
                headers = {"Authorization": f"Bearer {token(actor)}"}
                response = self.client.get(path, headers=headers)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["code"], "permission_required")
        self.assertEqual(self.upstreams.training_requests, [])

    def test_transport_validation_and_authentication(self) -> None:
        invalid = self.get("/v1/training-history", "athlete", limit=101)
        unknown_sport = self.get("/v1/training-history", "athlete", sport="golf")
        anonymous = self.get("/v1/training-history", None)

        self.assertEqual((invalid.status_code, unknown_sport.status_code), (422, 422))
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(self.upstreams.training_requests, [])

    def test_service_errors_and_failures(self) -> None:
        for status, code in ((403, "athlete_authorization_required"), (422, "validation_error")):
            with self.subTest(status=status):
                self.upstreams.training_response = httpx.Response(
                    status,
                    json={"code": code, "message": "m", "correlation_id": None, "details": []},
                )
                response = self.get(
                    f"/v1/athletes/{uuid.uuid4()}/training-history", "service_provider"
                )
                self.assertEqual((response.status_code, response.json()["code"]), (status, code))
        self.upstreams.training_down = httpx.ReadTimeout
        self.assertEqual(self.get("/v1/training-history", "athlete").status_code, 504)
        self.upstreams.training_down = httpx.ConnectError
        self.assertEqual(self.get("/v1/training-history", "athlete").status_code, 503)


if __name__ == "__main__":
    unittest.main()
