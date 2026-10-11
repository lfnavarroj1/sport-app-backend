"""BFF web, HU003: reenvío al servicio, IP del usuario y errores sin alterar."""

import json
import unittest
from typing import Any

import httpx
from fastapi.testclient import TestClient

from bff_web.main import create_app
from bff_web.settings import BffWebSettings
from sportapp_platform.correlation import CORRELATION_HEADER

SETTINGS = BffWebSettings(
    app_env="test",  # type: ignore[arg-type]
    users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
    cors_allowed_origins=("http://localhost:4200",),
)
PASSWORD = "contraseña-muy-secreta"
SESSION = {
    "token_type": "Bearer",
    "access_token": "eyJ.access.token",
    "expires_in": 900,
    "refresh_token": "r" * 43,
    "refresh_expires_in": 604800,
    "user": {
        "user_id": "6f1c1d7e-3b8a-4c1e-9a55-0d6c2f0b7a11",
        "full_name": "Ana Pérez",
        "email": "ana@example.com",
        "actor_type": "athlete",
    },
}


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


def session_ok(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=SESSION, headers={"Cache-Control": "no-store"})


class SessionsBffTest(unittest.TestCase):
    def test_login_is_forwarded_with_client_ip_and_correlation(self) -> None:
        """CA1: el BFF no verifica credenciales ni emite tokens; los reenvía."""
        client, upstream = client_with(session_ok)
        correlation_id = "1b4e28ba-2fa1-4d3b-a3f5-ef19b5a7633b"

        response = client.post(
            "/v1/sessions",
            json={"email": " ana@example.com ", "password": PASSWORD},
            headers={CORRELATION_HEADER: correlation_id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), SESSION)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        [sent] = upstream.requests
        self.assertEqual(sent.url.path, "/v1/sessions")
        self.assertEqual(sent.headers["X-Client-IP"], "testclient")
        self.assertEqual(sent.headers[CORRELATION_HEADER], correlation_id)
        self.assertEqual(
            json.loads(sent.content), {"email": "ana@example.com", "password": PASSWORD}
        )

    def test_service_errors_and_retry_after_are_passed_through(self) -> None:
        """CA2 a CA4: respuesta genérica y control de abuso del servicio propietario."""
        cases = [
            (401, {"code": "invalid_credentials"}, {}),
            (429, {"code": "too_many_attempts"}, {"Retry-After": "120"}),
        ]
        for status, body, headers in cases:
            with self.subTest(status=status):
                client, _ = client_with(
                    lambda _r, s=status, b=body, h=headers: httpx.Response(s, json=b, headers=h)
                )

                response = client.post(
                    "/v1/sessions", json={"email": "ana@example.com", "password": PASSWORD}
                )

                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json(), body)
                for name, value in headers.items():
                    self.assertEqual(response.headers[name], value)

    def test_refresh_is_forwarded_without_client_ip(self) -> None:
        client, upstream = client_with(session_ok)

        response = client.post("/v1/sessions/refresh", json={"refresh_token": "r" * 43})

        self.assertEqual(response.status_code, 200)
        [sent] = upstream.requests
        self.assertEqual(sent.url.path, "/v1/sessions/refresh")
        self.assertNotIn("X-Client-IP", sent.headers)

    def test_transport_validation_happens_before_calling_the_service(self) -> None:
        client, upstream = client_with(session_ok)

        responses = [
            client.post("/v1/sessions", json={"email": "ana@example.com"}),
            client.post("/v1/sessions", json={"email": "a@b.co", "password": PASSWORD, "x": 1}),
            client.post("/v1/sessions/refresh", json={"refresh_token": "corto"}),
        ]

        self.assertEqual([r.status_code for r in responses], [422, 422, 422])
        self.assertEqual(upstream.requests, [])
        self.assertNotIn(PASSWORD, "".join(r.text for r in responses))

    def test_unavailable_timeout_and_unexpected_status(self) -> None:
        def unavailable(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("caído")

        def timeout(_: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("lento")

        for handler, status, code in (
            (unavailable, 503, "upstream_unavailable"),
            (timeout, 504, "upstream_timeout"),
            (lambda _r: httpx.Response(500, json={}), 503, "upstream_unavailable"),
        ):
            with self.subTest(code=code, status=status):
                client, _ = client_with(handler)

                response = client.post("/v1/sessions/refresh", json={"refresh_token": "r" * 43})

                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["code"], code)

    def test_browser_can_read_retry_after_cross_origin(self) -> None:
        client, _ = client_with(
            lambda _r: httpx.Response(
                429, json={"code": "too_many_attempts"}, headers={"Retry-After": "60"}
            )
        )

        response = client.post(
            "/v1/sessions",
            json={"email": "ana@example.com", "password": PASSWORD},
            headers={"Origin": "http://localhost:4200"},
        )

        self.assertIn("retry-after", response.headers["access-control-expose-headers"].lower())


if __name__ == "__main__":
    unittest.main()
