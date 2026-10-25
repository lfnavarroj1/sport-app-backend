"""BFF móvil, HU043: catálogo de alimentos y registro de consumos (ADR-037).

El borde exige `athlete` (opción base, sin capacidad ni llamada a Afiliación); lo demás llega
del servicio sin alterar: `ETag`/`304`, `Idempotent-Replayed`, códigos y `details`.
"""

import json
import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_capabilities import Upstream, token

from bff_mobile.main import create_app
from bff_mobile.settings import BffMobileSettings

CATALOG_VERSION = "food-catalog-2026-10-13.v1"
ETAG = f'"{CATALOG_VERSION}"'
CATALOG = {
    "catalog_version": CATALOG_VERSION,
    "items": [
        {
            "food_code": "OATS",
            "label_key": "food.oats",
            "category": "GRAIN",
            "default_unit": "G",
            "allowed_units": ["G", "CUP", "TBSP"],
            "allergens": [],
            "incompatible_with": [],
        }
    ],
}
REQUEST = {
    "intake_id": str(uuid.uuid4()),
    "consumed_at": "2026-10-24T07:30:00Z",
    "meal_type": "BREAKFAST",
    "items": [{"food_code": "OATS", "quantity": "80", "unit": "G"}],
}
INTAKE = {
    **REQUEST,
    "athlete_id": str(uuid.uuid4()),
    "items": [{"food_code": "OATS", "quantity": "80.0", "unit": "G"}],
    "catalog_version": CATALOG_VERSION,
    "version": 1,
    "registered_at": "2026-10-24T07:31:02Z",
}
INTAKES = "/v1/me/food-intakes"


class FoodIntakeRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = Upstream()
        self.reply: httpx.Response = httpx.Response(200, json={})
        self.failure: type[Exception] | None = None
        self.requests: list[httpx.Request] = []

        def training(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.failure is not None:
                raise self.failure("entrenamientos")
            return self.reply

        settings = BffMobileSettings(
            app_env="test",  # type: ignore[arg-type]
            users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
            affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
            training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
            events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
            complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
        )
        self.client = TestClient(
            create_app(
                settings,
                users_http=httpx.Client(
                    base_url="http://users", transport=httpx.MockTransport(self.upstream.users)
                ),
                affiliation_http=httpx.Client(
                    base_url="http://affiliation",
                    transport=httpx.MockTransport(self.upstream.affiliation),
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

    def auth(self, actor: str = "athlete", **headers: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(actor)}", **headers}

    def register(self, body: Any = REQUEST, **headers: str) -> httpx.Response:
        return self.client.post(INTAKES, json=body, headers=self.auth(**headers))

    def test_catalog_returns_body_and_etag_without_affiliation(self) -> None:
        """CA5/CA6: el catálogo llega con su `ETag` para la caché del móvil; opción base sin
        consultar capacidades."""
        correlation = str(uuid.uuid4())
        self.reply = httpx.Response(200, json=CATALOG, headers={"ETag": ETAG})

        response = self.client.get(
            "/v1/food-catalog", headers=self.auth(**{"X-Correlation-ID": correlation})
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), CATALOG)
        self.assertEqual(response.headers["ETag"], ETAG)
        sent = self.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("GET", "/v1/food-catalog"))
        self.assertNotIn("If-None-Match", sent.headers)
        self.assertEqual(sent.headers["X-Correlation-ID"], correlation)
        self.assertTrue(sent.headers["Authorization"].startswith("Bearer "))
        self.assertEqual(self.upstream.affiliation_paths, [])

    def test_catalog_not_modified_is_relayed_without_body(self) -> None:
        self.reply = httpx.Response(304, headers={"ETag": ETAG})

        response = self.client.get("/v1/food-catalog", headers=self.auth(**{"If-None-Match": ETAG}))

        self.assertEqual((response.status_code, response.content), (304, b""))
        self.assertEqual(response.headers["ETag"], ETAG)
        self.assertEqual(self.requests[0].headers["If-None-Match"], ETAG)

    def test_register_forwards_body_key_and_correlation(self) -> None:
        """CA1: cuerpo y clave llegan tal cual; el BFF no normaliza ni añade `athlete_id`."""
        key, correlation = str(uuid.uuid4()), str(uuid.uuid4())
        self.reply = httpx.Response(201, json=INTAKE)

        response = self.register(**{"Idempotency-Key": key, "X-Correlation-ID": correlation})

        self.assertEqual((response.status_code, response.json()), (201, INTAKE))
        self.assertNotIn("Idempotent-Replayed", response.headers)
        sent = self.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("POST", INTAKES))
        self.assertEqual(json.loads(sent.content), REQUEST)
        self.assertEqual(sent.headers["Idempotency-Key"], key)
        self.assertEqual(sent.headers["X-Correlation-ID"], correlation)
        self.assertEqual(self.upstream.affiliation_paths, [])

    def test_replays_and_service_errors_are_relayed(self) -> None:
        """CA2/CA3: `Idempotent-Replayed`, `409` y `422` con `details` sin alterar."""
        self.reply = httpx.Response(201, json=INTAKE, headers={"Idempotent-Replayed": "true"})
        replayed = self.register(**{"Idempotency-Key": str(uuid.uuid4())})
        self.assertEqual(replayed.headers["Idempotent-Replayed"], "true")
        for status, body in (
            (
                409,
                {"code": "intake_id_reused", "message": "m", "correlation_id": "c", "details": []},
            ),
            (
                409,
                {
                    "code": "idempotency_key_reused",
                    "message": "m",
                    "correlation_id": "c",
                    "details": [],
                },
            ),
            (
                422,
                {
                    "code": "validation_error",
                    "message": "m",
                    "correlation_id": "c",
                    "details": [{"field": "athlete_id", "issue": "field_not_allowed"}],
                },
            ),
        ):
            with self.subTest(status=status, code=body["code"]):
                self.reply = httpx.Response(status, json=body)
                response = self.register({**REQUEST, "athlete_id": str(uuid.uuid4())})
                self.assertEqual((response.status_code, response.json()), (status, body))
        self.assertIn("athlete_id", json.loads(self.requests[-1].content))

    def test_only_athletes_reach_the_service(self) -> None:
        """Seguridad: otro actor → `403`; sin token o inválido → `401`; nada llega al servicio."""
        for actor in ("service_provider", "organizer"):
            with self.subTest(actor=actor):
                catalog = self.client.get("/v1/food-catalog", headers=self.auth(actor))
                intake = self.client.post(INTAKES, json=REQUEST, headers=self.auth(actor))
                for response in (catalog, intake):
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json()["code"], "permission_required")
        anonymous = self.client.post(INTAKES, json=REQUEST)
        forged = self.client.get("/v1/food-catalog", headers={"Authorization": "Bearer x.y.z"})
        self.assertEqual((anonymous.status_code, forged.status_code), (401, 401))
        self.assertEqual(self.requests, [])

    def test_upstream_failures_become_503_or_504(self) -> None:
        """La app reintenta solo ante red, `502`, `503` o `504` con la misma clave."""
        self.failure = httpx.ReadTimeout
        self.assertEqual(self.register().status_code, 504)
        self.failure = httpx.ConnectError
        self.assertEqual(self.client.get("/v1/food-catalog", headers=self.auth()).status_code, 503)
        self.failure = None
        self.reply = httpx.Response(500, json={"code": "boom"})
        with self.assertLogs("sportapp.bff", level="WARNING"):
            unexpected = self.register()
        self.assertEqual(
            (unexpected.status_code, unexpected.json()["code"]), (503, "upstream_unavailable")
        )
        self.upstream.jwks_down = True
        self.assertEqual(self.register().status_code, 503)


if __name__ == "__main__":
    unittest.main()
