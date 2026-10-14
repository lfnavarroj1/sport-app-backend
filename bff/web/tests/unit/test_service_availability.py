"""BFF web, HU020: catálogo de disponibilidad del deportista (ADR-038).

Filtro de borde por actor, reenvío literal de los parámetros (también los repetidos) y de
la correlación, y respuestas del servicio sin alterar.
"""

import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_training import Upstreams, token

from bff_web.main import create_app
from bff_web.settings import BffWebSettings
from sportapp_platform.correlation import CORRELATION_HEADER

CATALOG = {
    "items": [
        {
            "service_id": str(uuid.uuid4()),
            "provider_id": str(uuid.uuid4()),
            "name": "Entrenamiento funcional",
            "description": "Sesiones guiadas de entrenamiento funcional.",
            "category": "COACHING",
            "modality": "VIRTUAL",
            "price": {"amount": "120000.00", "currency": "COP", "billing_unit": "SESSION"},
            "availability": [
                {
                    "starts_at": "2026-11-02T13:00:00Z",
                    "ends_at": "2026-11-02T14:00:00Z",
                    "time_zone": "America/Bogota",
                    "capacity": 3,
                }
            ],
            "available": True,
        }
    ],
    "next_cursor": None,
    "restricted_categories": ["SPORTS_MEDICINE"],
}
PERIOD = {"from": "2026-11-01T00:00:00-05:00", "to": "2026-11-30T23:59:59-05:00"}


class ServiceAvailabilityRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstreams = Upstreams()
        self.requests: list[httpx.Request] = []
        self.response = httpx.Response(200, json=CATALOG)
        self.failure: type[Exception] | None = None

        def services(request: httpx.Request) -> httpx.Response:
            if self.failure is not None:
                raise self.failure("servicios")
            self.requests.append(request)
            return self.response

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
                services_http=httpx.Client(
                    transport=httpx.MockTransport(services), base_url="http://services"
                ),
            )
        )

    def get(self, params: Any = None, actor: str | None = "athlete", **headers: str) -> Any:
        sent = dict(headers)
        if actor is not None:
            sent["Authorization"] = f"Bearer {token(actor)}"
        return self.client.get("/v1/services/availability", params=params, headers=sent)

    def test_forwards_parameters_and_returns_the_catalog(self) -> None:
        """CA1 y CA6: catálogo y `restricted_categories` tal como los decide el servicio."""
        correlation = str(uuid.uuid4())
        params = [
            ("from", PERIOD["from"]),
            ("to", PERIOD["to"]),
            ("category", "COACHING"),
            ("category", "TRANSPORT"),
            ("modality", "VIRTUAL"),
            ("availability", "all"),
            ("limit", "10"),
            ("cursor", "abc"),
        ]

        response = self.get(params, **{CORRELATION_HEADER: correlation})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), CATALOG)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        [sent] = self.requests
        self.assertEqual(sent.url.path, "/v1/services/availability")
        self.assertEqual(sent.url.params.get_list("category"), ["COACHING", "TRANSPORT"])
        self.assertEqual(
            {name: sent.url.params[name] for name in ("from", "to", "availability", "limit")},
            {**PERIOD, "availability": "all", "limit": "10"},
        )
        self.assertEqual(sent.headers[CORRELATION_HEADER], correlation)
        self.assertTrue(sent.headers["Authorization"].startswith("Bearer "))

    def test_only_present_parameters_are_forwarded(self) -> None:
        self.get()

        self.assertEqual(list(self.requests[0].url.params.multi_items()), [])

    def test_service_errors_pass_through(self) -> None:
        cases = [
            (
                422,
                {
                    "code": "validation_error",
                    "details": [{"field": "query.to", "issue": "out_of_range"}],
                },
            ),
            (503, {"code": "upstream_unavailable", "details": []}),
            (403, {"code": "permission_required", "details": []}),
        ]
        for status, body in cases:
            self.response = httpx.Response(
                status, json={"message": "m", "correlation_id": None, **body}
            )
            with self.subTest(status=status):
                response = self.get(PERIOD)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["details"], body["details"])

    def test_edge_rejections_never_reach_the_service(self) -> None:
        provider = self.get(PERIOD, actor="service_provider")
        organizer = self.get(PERIOD, actor="organizer")
        anonymous = self.get(PERIOD, actor=None)

        self.assertEqual(
            (provider.status_code, provider.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(organizer.status_code, 403)
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(self.requests, [])

    def test_upstream_failures(self) -> None:
        self.failure = httpx.ReadTimeout
        timeout = self.get(PERIOD)
        self.failure = httpx.ConnectError
        down = self.get(PERIOD)
        self.failure = None
        self.response = httpx.Response(500, json={"code": "internal_error"})
        unexpected = self.get(PERIOD)

        self.assertEqual((timeout.status_code, timeout.json()["code"]), (504, "upstream_timeout"))
        self.assertEqual((down.status_code, down.json()["code"]), (503, "upstream_unavailable"))
        self.assertEqual(
            (unexpected.status_code, unexpected.json()["code"]), (503, "upstream_unavailable")
        )


if __name__ == "__main__":
    unittest.main()
