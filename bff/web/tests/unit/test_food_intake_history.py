"""BFF web, HU013: filtro de borde, reenvío de filtros y errores del historial de alimentación."""

import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_training import Upstreams, token

from bff_web.main import create_app
from bff_web.settings import BffWebSettings

OWN = "/v1/me/food-intakes"
INTAKE = {
    "intake_id": "3f0c5d0e-6a8f-4a8e-9a43-0c7f0c6b1a10",
    "athlete_id": "8a3c1f51-2b4e-4c55-8f1a-7d1e9b0a2c33",
    "consumed_at": "2026-10-24T12:30:00Z",
    "meal_type": "LUNCH",
    "items": [{"food_code": "WHITE_RICE", "quantity": "150.0", "unit": "G"}],
    "catalog_version": "food-catalog-2026-10-13.v1",
    "version": 1,
    "registered_at": "2026-10-24T12:31:00Z",
}
PAGE = {"items": [INTAKE], "next_cursor": "c2lndWllbnRl"}


class FoodIntakeHistoryRoutesTest(unittest.TestCase):
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

    def test_own_history_forwards_filters_and_token(self) -> None:
        """CA1: el deportista recibe los consumos del servicio sin alterar."""
        response = self.get(
            OWN,
            "athlete",
            **{
                "from": "2026-10-01",
                "to": "2026-10-31",
                "meal_type": "LUNCH",
                "limit": 5,
                "cursor": "abc",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), PAGE)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        [forwarded] = self.upstreams.training_requests
        self.assertEqual(forwarded.url.path, OWN)
        self.assertEqual(
            dict(forwarded.url.params),
            {
                "from": "2026-10-01",
                "to": "2026-10-31",
                "meal_type": "LUNCH",
                "limit": "5",
                "cursor": "abc",
            },
        )
        self.assertTrue(forwarded.headers["Authorization"].startswith("Bearer "))

    def test_default_limit_and_athlete_route(self) -> None:
        athlete_id = uuid.uuid4()

        response = self.get(f"/v1/athletes/{athlete_id}/food-intakes", "service_provider")

        self.assertEqual(response.status_code, 200)
        [forwarded] = self.upstreams.training_requests
        self.assertEqual(forwarded.url.path, f"/v1/athletes/{athlete_id}/food-intakes")
        self.assertEqual(dict(forwarded.url.params), {"limit": "20"})

    def test_empty_page_is_relayed(self) -> None:
        """CA5: colección vacía explícita."""
        self.upstreams.training_response = httpx.Response(
            200, json={"items": [], "next_cursor": None}
        )

        response = self.get(OWN, "athlete")

        self.assertEqual(response.json(), {"items": [], "next_cursor": None})

    def test_edge_filters_by_actor_and_permission(self) -> None:
        """Defensa en el borde; la autorización vigente la decide el servicio (CA3)."""
        athlete_path = f"/v1/athletes/{uuid.uuid4()}/food-intakes"
        cases = (
            (OWN, "service_provider"),
            (OWN, "organizer"),
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
        """CA2 en el borde: límite, tipo de comida o fecha mal formados no llegan al servicio."""
        cases = ({"limit": 101}, {"limit": 0}, {"meal_type": "BRUNCH"}, {"from": "ayer"})
        for params in cases:
            with self.subTest(params=params):
                response = self.get(OWN, "athlete", **params)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "validation_error")
        self.assertEqual(self.get(OWN, None).status_code, 401)
        self.assertEqual(self.upstreams.training_requests, [])

    def test_service_errors_and_failures(self) -> None:
        for status, code in ((403, "athlete_authorization_required"), (422, "validation_error")):
            with self.subTest(status=status):
                self.upstreams.training_response = httpx.Response(
                    status,
                    json={"code": code, "message": "m", "correlation_id": None, "details": []},
                )
                response = self.get(f"/v1/athletes/{uuid.uuid4()}/food-intakes", "service_provider")
                self.assertEqual((response.status_code, response.json()["code"]), (status, code))
        self.upstreams.training_down = httpx.ReadTimeout
        self.assertEqual(self.get(OWN, "athlete").status_code, 504)
        self.upstreams.training_down = httpx.ConnectError
        self.assertEqual(self.get(OWN, "athlete").status_code, 503)


if __name__ == "__main__":
    unittest.main()
