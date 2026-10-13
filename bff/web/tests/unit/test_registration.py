"""BFF web, HU001: adaptación, propagación y normalización de errores del servicio."""

import json
import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient

from bff_web.main import create_app
from bff_web.settings import BffWebSettings
from sportapp_platform.correlation import CORRELATION_HEADER

SETTINGS = BffWebSettings(
    app_env="test",  # type: ignore[arg-type]
    users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
    affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
    training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
    complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
)
PASSWORD = "contraseña-muy-secreta"
CREATED = {
    "user_id": "6f1c1d7e-3b8a-4c1e-9a55-0d6c2f0b7a11",
    "full_name": "Ana Pérez",
    "email": "ana@example.com",
    "actor_type": "athlete",
    "status": "active",
    "aggregate_version": 1,
    "created_at": "2026-10-07T12:00:00+00:00",
}


def body(**overrides: Any) -> dict[str, Any]:
    values = {
        "full_name": "Ana Pérez",
        "email": "ana@example.com",
        "password": PASSWORD,
        "actor_type": "athlete",
        "accepted_policies_version": "2026-10",
    }
    values.update(overrides)
    return values


class UpstreamDouble:
    def __init__(self, handler: Any) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result: httpx.Response = self._handler(request)
        return result


def client_with(handler: Any) -> tuple[TestClient, UpstreamDouble]:
    upstream = UpstreamDouble(handler)
    http = httpx.Client(base_url="http://users.invalid", transport=httpx.MockTransport(upstream))
    return TestClient(create_app(SETTINGS, users_http=http)), upstream


class RegistrationBffTest(unittest.TestCase):
    def post(self, client: TestClient, payload: dict[str, Any], **headers: str) -> httpx.Response:
        headers = {"Idempotency-Key": str(uuid.uuid4()), **headers}
        return client.post("/v1/users", json=payload, headers=headers)

    def test_forwards_command_with_idempotency_and_correlation(self) -> None:
        client, upstream = client_with(
            lambda _: httpx.Response(
                201,
                json=CREATED,
                headers={"Location": "/v1/users/x", "Idempotent-Replayed": "true"},
            )
        )
        key, correlation_id = str(uuid.uuid4()), str(uuid.uuid4())

        response = self.post(
            client, body(), **{"Idempotency-Key": key, CORRELATION_HEADER: correlation_id}
        )

        [forwarded] = upstream.requests
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), CREATED)
        self.assertEqual(response.headers["Location"], "/v1/users/x")
        self.assertEqual(response.headers["Idempotent-Replayed"], "true")
        self.assertEqual(forwarded.url.path, "/v1/users")
        self.assertEqual(forwarded.headers["Idempotency-Key"], key)
        self.assertEqual(forwarded.headers[CORRELATION_HEADER], correlation_id)
        self.assertEqual(json.loads(forwarded.content)["password"], PASSWORD)

    def test_business_errors_from_service_are_passed_through(self) -> None:
        for status, code in ((409, "email_already_registered"), (422, "policies_version_mismatch")):
            error = {"code": code, "message": "m", "correlation_id": None, "details": []}
            client, _ = client_with(lambda _, s=status, e=error: httpx.Response(s, json=e))
            with self.subTest(code=code):
                response = self.post(client, body())

                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["code"], code)

    def test_transport_validation_happens_before_calling_service(self) -> None:
        client, upstream = client_with(lambda _: httpx.Response(201, json=CREATED))

        response = self.post(client, body(password="corta", actor_type="admin"))

        self.assertEqual(response.status_code, 422)
        self.assertEqual(upstream.requests, [])
        self.assertNotIn("corta", response.text)

    def test_missing_idempotency_key_is_rejected_by_bff(self) -> None:
        client, upstream = client_with(lambda _: httpx.Response(201, json=CREATED))

        response = client.post("/v1/users", json=body())

        self.assertEqual(response.status_code, 422)
        self.assertEqual(upstream.requests, [])

    def test_unavailable_or_failing_service_returns_503(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        for handler in (refuse, lambda _: httpx.Response(500, json={"code": "internal_error"})):
            client, _ = client_with(handler)
            with self.subTest(handler=handler), self.assertNoLogs("sportapp.errors"):
                response = self.post(client, body())

                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["code"], "upstream_unavailable")

    def test_timeout_returns_504(self) -> None:
        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        client, _ = client_with(slow)

        response = self.post(client, body())

        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["code"], "upstream_timeout")
        self.assertNotIn(PASSWORD, response.text)


if __name__ == "__main__":
    unittest.main()
