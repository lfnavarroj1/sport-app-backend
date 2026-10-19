"""BFF web, HU011 (CA5): composición de `GET /v1/me/training-athletes`.

Entrenamientos entrega la página de autorizaciones vigentes y Usuarios el nombre y el
correo por lote; el BFF solo une ambas respuestas en el orden de Entrenamientos.
"""

import json
import logging
import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_training import Upstreams, token

from bff_web.main import create_app
from bff_web.settings import BffWebSettings

ANA = str(uuid.uuid4())
LUIS = str(uuid.uuid4())
DISPLAYS = {
    ANA: {"user_id": ANA, "full_name": "Ana Pérez", "email": "ana@example.com"},
    LUIS: {"user_id": LUIS, "full_name": "Luis Gómez", "email": "luis@example.com"},
}


def authorized_page(*athlete_ids: str, next_cursor: str | None = None) -> dict[str, Any]:
    return {
        "items": [
            {"athlete_id": athlete_id, "authorized_at": f"2026-10-1{index}T08:00:00Z"}
            for index, athlete_id in enumerate(athlete_ids)
        ],
        "next_cursor": next_cursor,
    }


class BatchUpstreams(Upstreams):
    def __init__(self) -> None:
        super().__init__()
        self.batches: list[list[str]] = []
        self.displays: dict[str, dict[str, str]] = dict(DISPLAYS)
        self.batch_status = 200
        self.batch_down: type[Exception] | None = None

    def users(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/users/lookup-batch":
            return super().users(request)
        if self.batch_down is not None:
            raise self.batch_down("usuarios")
        ids: list[str] = json.loads(request.content)["user_ids"]
        self.batches.append(ids)
        if self.batch_status != 200:
            return httpx.Response(self.batch_status)
        items = [self.displays[user_id] for user_id in ids if user_id in self.displays]
        return httpx.Response(200, json={"items": items})


class TrainingAthletesRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstreams = BatchUpstreams()
        self.upstreams.training_response = httpx.Response(
            200, json=authorized_page(LUIS, ANA, next_cursor="siguiente")
        )
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

    def get(self, actor: str | None = "service_provider", **params: Any) -> Any:
        headers = {"Authorization": f"Bearer {token(actor)}"} if actor else {}
        return self.client.get("/v1/me/training-athletes", headers=headers, params=params)

    def test_composes_page_in_training_order(self) -> None:
        response = self.get(limit=2, cursor="abc")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(
            response.json(),
            {
                "items": [
                    {
                        "athlete_id": LUIS,
                        "full_name": "Luis Gómez",
                        "email": "luis@example.com",
                        "authorized_at": "2026-10-10T08:00:00Z",
                    },
                    {
                        "athlete_id": ANA,
                        "full_name": "Ana Pérez",
                        "email": "ana@example.com",
                        "authorized_at": "2026-10-11T08:00:00Z",
                    },
                ],
                "next_cursor": "siguiente",
            },
        )
        [forwarded] = self.upstreams.training_requests
        self.assertEqual(forwarded.url.path, "/v1/me/authorized-athletes")
        self.assertEqual(dict(forwarded.url.params), {"limit": "2", "cursor": "abc"})
        self.assertTrue(forwarded.headers["Authorization"].startswith("Bearer "))
        self.assertEqual(self.upstreams.batches, [[LUIS, ANA]])

    def test_ids_come_only_from_the_training_page(self) -> None:
        """El navegador no puede elegir a quién enriquecer: los parámetros extra se ignoran."""
        other = str(uuid.uuid4())

        response = self.client.get(
            "/v1/me/training-athletes",
            headers={"Authorization": f"Bearer {token('service_provider')}"},
            params={"user_ids": other, "athlete_id": other},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.upstreams.batches, [[LUIS, ANA]])
        self.assertEqual(dict(self.upstreams.training_requests[0].url.params), {"limit": "20"})

    def test_empty_page_does_not_call_users(self) -> None:
        self.upstreams.training_response = httpx.Response(200, json=authorized_page())

        response = self.get()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": [], "next_cursor": None})
        self.assertEqual(self.upstreams.batches, [])

    def test_incomplete_enrichment_is_503_without_identifiers_or_personal_data(self) -> None:
        del self.upstreams.displays[ANA]

        with self.assertLogs("sportapp.bff", level=logging.WARNING) as logs:
            response = self.get()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "upstream_unavailable")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        for forbidden in (ANA, LUIS, "Luis Gómez", "luis@example.com"):
            self.assertNotIn(forbidden, response.text)
            self.assertNotIn(forbidden, "\n".join(logs.output))

    def test_users_failures(self) -> None:
        cases: tuple[tuple[str, Any, int, str], ...] = (
            ("caído", httpx.ConnectError, 503, "upstream_unavailable"),
            ("lento", httpx.ReadTimeout, 504, "upstream_timeout"),
            ("error", 500, 503, "upstream_unavailable"),
            ("rechazo", 422, 503, "upstream_unavailable"),
        )
        for name, failure, status, code in cases:
            with self.subTest(case=name):
                if isinstance(failure, int):
                    self.upstreams.batch_down, self.upstreams.batch_status = None, failure
                else:
                    self.upstreams.batch_down, self.upstreams.batch_status = failure, 200
                response = self.get()
                self.assertEqual((response.status_code, response.json()["code"]), (status, code))
                self.assertNotIn(LUIS, response.text)

    def test_edge_filters_by_actor_and_permission(self) -> None:
        for actor in ("athlete", "organizer", None):
            with self.subTest(actor=actor):
                response = self.client.get(
                    "/v1/me/training-athletes",
                    headers={"Authorization": f"Bearer {token(actor)}"},
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["code"], "permission_required")
        self.assertEqual(self.upstreams.training_requests, [])

    def test_transport_validation_and_authentication(self) -> None:
        for limit in (0, 101, "x"):
            with self.subTest(limit=limit):
                self.assertEqual(self.get(limit=limit).status_code, 422)
        self.assertEqual(self.get(None).status_code, 401)
        self.assertEqual(self.upstreams.training_requests, [])

    def test_training_errors_and_failures(self) -> None:
        for status, code in ((403, "permission_required"), (422, "validation_error")):
            with self.subTest(status=status):
                self.upstreams.training_response = httpx.Response(
                    status,
                    json={"code": code, "message": "m", "correlation_id": None, "details": []},
                )
                response = self.get()
                self.assertEqual((response.status_code, response.json()["code"]), (status, code))
        self.upstreams.training_down = httpx.ReadTimeout
        self.assertEqual(self.get().status_code, 504)
        self.upstreams.training_down = httpx.ConnectError
        self.assertEqual(self.get().status_code, 503)
        self.assertEqual(self.upstreams.batches, [])


if __name__ == "__main__":
    unittest.main()
