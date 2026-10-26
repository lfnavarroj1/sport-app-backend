"""BFF web, HU012: filtro de borde, reenvío de filtros y errores del historial de salud."""

import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_training import Upstreams, token

from bff_web.main import create_app
from bff_web.settings import BffWebSettings

REPORT = {
    "report_id": str(uuid.uuid4()),
    "type": "INJURY",
    "body_area": "KNEE",
    "severity": "HIGH",
    "occurred_at": "2026-10-24T18:30:00Z",
    "expected_recovery_days": 7,
    "status": "OPEN",
    "session_id": None,
}
PAGE = {"items": [REPORT], "next_cursor": None}


class HealthHistoryRoutesTest(unittest.TestCase):
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
                    events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
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

    def test_own_reports_forward_filters_and_token(self) -> None:
        """CA1: el deportista consulta los suyos con periodo, tipo y severidad."""
        response = self.get(
            "/v1/me/health-reports",
            "athlete",
            **{
                "from": "2026-10-01",
                "to": "2026-10-31",
                "type": "INJURY",
                "severity": "HIGH",
                "limit": 5,
                "cursor": "abc",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), PAGE)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        [forwarded] = self.upstreams.training_requests
        self.assertEqual(forwarded.url.path, "/v1/me/health-reports")
        self.assertEqual(
            dict(forwarded.url.params),
            {
                "from": "2026-10-01",
                "to": "2026-10-31",
                "type": "INJURY",
                "severity": "HIGH",
                "limit": "5",
                "cursor": "abc",
            },
        )
        self.assertTrue(forwarded.headers["Authorization"].startswith("Bearer "))

    def test_default_limit_and_athlete_route(self) -> None:
        """CA2: el profesional con el permiso llega al servicio, que decide la autorización."""
        athlete_id = uuid.uuid4()

        response = self.get(f"/v1/athletes/{athlete_id}/health-reports", "service_provider")

        self.assertEqual(response.status_code, 200)
        [forwarded] = self.upstreams.training_requests
        self.assertEqual(forwarded.url.path, f"/v1/athletes/{athlete_id}/health-reports")
        self.assertEqual(dict(forwarded.url.params), {"limit": "20"})

    def test_empty_collection_is_forwarded(self) -> None:
        """CA4."""
        self.upstreams.training_response = httpx.Response(
            200, json={"items": [], "next_cursor": None}
        )

        response = self.get("/v1/me/health-reports", "athlete")

        self.assertEqual(response.json(), {"items": [], "next_cursor": None})

    def test_edge_filters_by_actor_and_permission(self) -> None:
        """CA3: defensa en el borde; la autorización vigente la decide el servicio."""
        athlete_path = f"/v1/athletes/{uuid.uuid4()}/health-reports"
        cases = (
            ("/v1/me/health-reports", "service_provider"),
            ("/v1/me/health-reports", "organizer"),
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
        """CA6 en el borde: filtros de catálogo y límite inválidos no llegan al servicio."""
        cases = (
            {"limit": 101},
            {"limit": 0},
            {"type": "FRACTURE"},
            {"severity": "CRITICAL"},
            {"from": "ayer"},
        )
        for params in cases:
            with self.subTest(params=params):
                response = self.get("/v1/me/health-reports", "athlete", **params)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "validation_error")
        anonymous = self.get("/v1/me/health-reports", None)

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
                    f"/v1/athletes/{uuid.uuid4()}/health-reports", "service_provider"
                )
                self.assertEqual((response.status_code, response.json()["code"]), (status, code))
        self.upstreams.training_down = httpx.ReadTimeout
        self.assertEqual(self.get("/v1/me/health-reports", "athlete").status_code, 504)
        self.upstreams.training_down = httpx.ConnectError
        self.assertEqual(self.get("/v1/me/health-reports", "athlete").status_code, 503)


if __name__ == "__main__":
    unittest.main()
