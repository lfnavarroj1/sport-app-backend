"""BFF web, HU015: filtro de borde, reenvío literal del cuerpo y errores del servicio."""

import json
import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_training import Upstreams, token

from bff_web.main import create_app
from bff_web.settings import BffWebSettings

BODY = {
    "name": "Valoración deportiva inicial",
    "description": "Valoración presencial para orientar el plan deportivo.",
    "category": "SPORTS_MEDICINE",
    "modality": "IN_PERSON",
    "price": {"amount": "120000.00", "currency": "COP", "billing_unit": "SESSION"},
}


class ServicesRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstreams = Upstreams()
        self.requests: list[httpx.Request] = []
        self.response = httpx.Response(
            201,
            json={"service_id": str(uuid.uuid4())},
            headers={"Location": "/v1/services/x", "Idempotent-Replayed": "true"},
        )
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
                ),
                users_http=httpx.Client(
                    transport=httpx.MockTransport(self.upstreams.users), base_url="http://users"
                ),
                services_http=httpx.Client(
                    transport=httpx.MockTransport(services), base_url="http://services"
                ),
            )
        )

    def post(self, body: Any, actor: str | None = "service_provider", **headers: str) -> Any:
        sent = dict(headers)
        if actor is not None:
            sent["Authorization"] = f"Bearer {token(actor)}"
        return self.client.post("/v1/services", json=body, headers=sent)

    def test_forwards_body_key_and_token_verbatim(self) -> None:
        """El BFF no reinterpreta la oferta: las reglas son del servicio (ADR-027)."""
        key = str(uuid.uuid4())
        body = {**BODY, "provider_id": "inyectado"}

        response = self.post(body, **{"Idempotency-Key": key})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers["Location"], "/v1/services/x")
        self.assertEqual(response.headers["Idempotent-Replayed"], "true")
        [sent] = self.requests
        self.assertEqual(json.loads(sent.content), body)
        self.assertEqual(sent.headers["Idempotency-Key"], key)
        self.assertTrue(sent.headers["Authorization"].startswith("Bearer "))

    def test_missing_key_is_forwarded_for_the_service_to_report(self) -> None:
        self.post(BODY)

        self.assertNotIn("Idempotency-Key", self.requests[0].headers)

    def test_edge_permission(self) -> None:
        """CA2: solo `service_provider` tiene `services.create` en web."""
        for actor in ("athlete", "organizer"):
            with self.subTest(actor=actor):
                response = self.post(BODY, actor)
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )
        self.assertEqual(self.post(BODY, None).status_code, 401)
        self.assertEqual(self.requests, [])

    def test_service_errors_pass_through(self) -> None:
        detail = {"field": "provider_id", "issue": "field_not_allowed"}
        for status, payload in (
            (
                422,
                {
                    "code": "validation_error",
                    "message": "m",
                    "correlation_id": None,
                    "details": [detail],
                },
            ),
            (
                409,
                {
                    "code": "idempotency_key_reused",
                    "message": "m",
                    "correlation_id": None,
                    "details": [],
                },
            ),
            (
                403,
                {
                    "code": "permission_required",
                    "message": "m",
                    "correlation_id": None,
                    "details": [],
                },
            ),
        ):
            with self.subTest(status=status):
                self.response = httpx.Response(status, json=payload)
                response = self.post(BODY)
                self.assertEqual((response.status_code, response.json()), (status, payload))

    def test_failures(self) -> None:
        self.failure = httpx.ReadTimeout
        timeout = self.post(BODY)
        self.failure = httpx.ConnectError
        down = self.post(BODY)
        self.failure = None
        self.response = httpx.Response(500)
        with self.assertLogs("sportapp.bff", level="WARNING"):
            unexpected = self.post(BODY)

        self.assertEqual((timeout.status_code, timeout.json()["code"]), (504, "upstream_timeout"))
        self.assertEqual(down.status_code, 503)
        self.assertEqual(unexpected.status_code, 503)


if __name__ == "__main__":
    unittest.main()
