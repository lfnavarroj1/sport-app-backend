"""BFF móvil: `GET /v1/services/availability` (ADR-038) para HU040 (ADR-046).

Solo el deportista en el borde, sin consultar Afiliación (la capacidad médica la decide el
servicio y la declara en `restricted_categories`). Los parámetros se reenvían tal como
llegan, incluidos `category` y `modality` repetidos; la respuesta, con el `availability_id`
de cada intervalo, llega sin alterar y sin caché.
"""

import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_capabilities import token
from test_devices import SlowableUpstream, error

from bff_mobile.api.service_availability import build_service_availability_router
from bff_mobile.clients.complementary_services import ComplementaryServicesClient
from bff_mobile.clients.users_management import UsersManagementClient
from bff_mobile.settings import BffMobileSettings
from sportapp_platform.app import create_service_app
from sportapp_platform.tokens import CachedJwksVerifier

PATH = "/v1/services/availability"
PAGE: dict[str, Any] = {
    "items": [
        {
            "service_id": str(uuid.uuid4()),
            "provider_id": str(uuid.uuid4()),
            "name": "Entrenamiento virtual",
            "description": "Sesión guiada por videollamada coordinada por el profesional.",
            "category": "COACHING",
            "modality": "VIRTUAL",
            "price": {"amount": "80000.00", "currency": "COP", "billing_unit": "SESSION"},
            "availability": [
                {
                    "starts_at": "2026-11-02T13:00:00Z",
                    "ends_at": "2026-11-02T14:00:00Z",
                    "time_zone": "America/Bogota",
                    "capacity": 1,
                    "availability_id": str(uuid.uuid4()),
                }
            ],
            "available": True,
        }
    ],
    "next_cursor": None,
    "restricted_categories": ["SPORTS_MEDICINE"],
}


class ServiceAvailabilityRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = SlowableUpstream({"subscription_status": "NONE", "capabilities": []})
        self.reply: httpx.Response = httpx.Response(200, json=PAGE)
        self.failure: type[Exception] | None = None
        self.requests: list[httpx.Request] = []

        def services(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.failure is not None:
                raise self.failure("servicios")
            return self.reply

        settings = BffMobileSettings(
            app_env="test",  # type: ignore[arg-type]
            users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
            affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
            training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
            events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
            complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
        )
        users = UsersManagementClient(
            httpx.Client(
                base_url="http://users", transport=httpx.MockTransport(self.upstream.users)
            )
        )
        verifier = CachedJwksVerifier(
            users.jwks, issuer=settings.jwt_issuer, audience=settings.jwt_audience
        )
        app = create_service_app(
            service_name="bff-mobile",
            title="SportApp - BFF móvil",
            api_version="test",
            settings=settings,
            routers=[
                build_service_availability_router(
                    verify_token=verifier.verify,
                    services=ComplementaryServicesClient(
                        httpx.Client(
                            base_url="http://services", transport=httpx.MockTransport(services)
                        )
                    ),
                )
            ],
        )
        self.client = TestClient(app)

    def auth(self, actor: str = "athlete", **headers: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(actor)}", **headers}

    def test_page_is_relayed_unchanged_with_restricted_categories_and_no_cache(self) -> None:
        correlation = str(uuid.uuid4())
        user_token = token("athlete")

        response = self.client.get(
            PATH,
            params={"from": "2026-11-01T00:00:00Z", "to": "2026-11-30T00:00:00Z"},
            headers={"Authorization": f"Bearer {user_token}", "X-Correlation-ID": correlation},
        )

        self.assertEqual((response.status_code, response.json()), (200, PAGE))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json()["restricted_categories"], ["SPORTS_MEDICINE"])
        sent = self.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("GET", PATH))
        self.assertEqual(sent.headers["Authorization"], f"Bearer {user_token}")
        self.assertEqual(sent.headers["X-Correlation-ID"], correlation)
        # La capacidad médica la decide el servicio: el BFF no consulta Afiliación.
        self.assertEqual(self.upstream.affiliation_paths, [])

    def test_parameters_are_forwarded_as_they_arrive_including_repeated_ones(self) -> None:
        query = [
            ("from", "2026-11-01T00:00:00-05:00"),
            ("to", "2026-11-30T00:00:00Z"),
            ("category", "COACHING"),
            ("category", "SPORTS_MEDICINE"),
            ("modality", "VIRTUAL"),
            ("modality", "HYBRID"),
            ("availability", "all"),
            ("limit", "abc"),
            ("cursor", "c1"),
        ]

        self.client.get(PATH, params=query, headers=self.auth())

        sent = self.requests[0].url.params
        self.assertEqual(sent.get_list("category"), ["COACHING", "SPORTS_MEDICINE"])
        self.assertEqual(sent.get_list("modality"), ["VIRTUAL", "HYBRID"])
        self.assertEqual(sorted(sent.multi_items()), sorted(query))

    def test_only_present_parameters_are_forwarded(self) -> None:
        self.client.get(PATH, params={"from": "2026-11-01T00:00:00Z"}, headers=self.auth())

        self.assertEqual(
            self.requests[0].url.params.multi_items(), [("from", "2026-11-01T00:00:00Z")]
        )

    def test_service_errors_pass_through_without_cache(self) -> None:
        cases = [
            (
                422,
                error("validation_error", [{"field": "query.category", "issue": "unknown_value"}]),
            ),
            (422, error("validation_error", [{"field": "query.to", "issue": "out_of_range"}])),
            (403, error("permission_required")),
            (401, error("invalid_access_token")),
            (503, error("upstream_unavailable")),
        ]
        for status, body in cases:
            self.reply = httpx.Response(status, json=body)
            response = self.client.get(PATH, headers=self.auth())
            with self.subTest(status=status, details=body["details"]):
                self.assertEqual((response.status_code, response.json()), (status, body))
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_other_actor_or_no_token_never_reach_the_service(self) -> None:
        for actor in ("organizer", "service_provider"):
            response = self.client.get(PATH, headers=self.auth(actor))
            with self.subTest(actor=actor):
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )
        response = self.client.get(PATH)
        self.assertEqual(
            (response.status_code, response.json()["code"]), (401, "invalid_access_token")
        )
        self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(self.requests, [])

    def test_jwks_unavailable_is_503(self) -> None:
        self.upstream.jwks_down = True
        response = self.client.get(PATH, headers=self.auth())
        self.assertEqual(
            (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
        )

    def test_service_unreachable_or_unexpected(self) -> None:
        self.failure = httpx.ReadTimeout
        timeout = self.client.get(PATH, headers=self.auth())
        self.failure = httpx.ConnectError
        down = self.client.get(PATH, headers=self.auth())
        self.failure = None
        self.reply = httpx.Response(500, json=error("internal_error"))
        unexpected = self.client.get(PATH, headers=self.auth())

        self.assertEqual((timeout.status_code, timeout.json()["code"]), (504, "upstream_timeout"))
        self.assertEqual((down.status_code, down.json()["code"]), (503, "upstream_unavailable"))
        self.assertEqual(
            (unexpected.status_code, unexpected.json()["code"]), (503, "upstream_unavailable")
        )


if __name__ == "__main__":
    unittest.main()
