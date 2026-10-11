"""BFF web: CORS para el cliente Angular servido desde otro origen."""

import os
import unittest
import uuid
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from bff_web.main import create_app
from bff_web.settings import BffWebSettings
from sportapp_platform.config import ConfigurationError, load_settings

WEB_CONTAINER = "http://localhost:18082"
WEB_DEV_SERVER = "http://localhost:4200"
OTHER_ORIGIN = "http://evil.invalid"
CREATED = {
    "user_id": "6f1c1d7e-3b8a-4c1e-9a55-0d6c2f0b7a11",
    "full_name": "Ana Pérez",
    "email": "ana@example.com",
    "actor_type": "athlete",
    "status": "active",
    "aggregate_version": 1,
    "created_at": "2026-10-07T12:00:00+00:00",
}
BODY = {
    "full_name": "Ana Pérez",
    "email": "ana@example.com",
    "password": "contraseña-muy-secreta",
    "actor_type": "athlete",
    "accepted_policies_version": "2026-10",
}


def settings(origins: str | None) -> BffWebSettings:
    environment = {"APP_ENV": "test", "USERS_MANAGEMENT_BASE_URL": "http://users.invalid"}
    if origins is not None:
        environment["CORS_ALLOWED_ORIGINS"] = origins
    with mock.patch.dict(os.environ, environment, clear=True):
        return load_settings(BffWebSettings)


def created(_: httpx.Request) -> httpx.Response:
    return httpx.Response(
        201, json=CREATED, headers={"Location": f"/v1/users/{CREATED['user_id']}"}
    )


def client(origins: str | None) -> TestClient:
    http = httpx.Client(base_url="http://users.invalid", transport=httpx.MockTransport(created))
    return TestClient(create_app(settings(origins), users_http=http))


def preflight(test_client: TestClient, origin: str) -> httpx.Response:
    return test_client.options(
        "/v1/users",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,idempotency-key,x-correlation-id",
        },
    )


class CorsSettingsTest(unittest.TestCase):
    def test_origins_are_read_as_comma_separated_list(self) -> None:
        loaded = settings(f" {WEB_CONTAINER} , {WEB_DEV_SERVER},")

        self.assertEqual(loaded.cors_allowed_origins, (WEB_CONTAINER, WEB_DEV_SERVER))

    def test_no_origins_by_default(self) -> None:
        self.assertEqual(settings(None).cors_allowed_origins, ())
        self.assertEqual(settings("").cors_allowed_origins, ())

    def test_invalid_origins_fail_at_startup_naming_only_the_variable(self) -> None:
        for invalid in (
            "*",
            "https://*.example.com",
            "ftp://localhost:4200",
            "localhost:4200",
            "http://localhost:4200/",
            "http://localhost:4200/app",
            "http://user:secret@localhost:4200",
        ):
            with self.subTest(origin=invalid):
                with self.assertRaises(ConfigurationError) as error:
                    settings(invalid)
                self.assertIn("CORS_ALLOWED_ORIGINS", str(error.exception))
                self.assertNotIn("secret", str(error.exception))


class CorsMiddlewareTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = client(f"{WEB_CONTAINER},{WEB_DEV_SERVER}")

    def test_preflight_from_allowed_origin_permits_registration_headers(self) -> None:
        for origin in (WEB_CONTAINER, WEB_DEV_SERVER):
            with self.subTest(origin=origin):
                response = preflight(self.client, origin)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["access-control-allow-origin"], origin)
                self.assertIn("POST", response.headers["access-control-allow-methods"])
                allowed = response.headers["access-control-allow-headers"].lower()
                for header in ("content-type", "idempotency-key", "x-correlation-id"):
                    self.assertIn(header, allowed)
                # HU003 0.4.0: el navegador envía la cookie del refresh token.
                self.assertEqual(response.headers["access-control-allow-credentials"], "true")

    def test_preflight_from_unknown_origin_is_rejected(self) -> None:
        response = preflight(self.client, OTHER_ORIGIN)

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_preflight_for_unlisted_header_is_rejected(self) -> None:
        response = self.client.options(
            "/v1/users",
            headers={
                "Origin": WEB_CONTAINER,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization",
            },
        )

        self.assertEqual(response.status_code, 400)

    def test_registration_response_exposes_contract_headers_to_browser(self) -> None:
        response = self.client.post(
            "/v1/users",
            json=BODY,
            headers={"Origin": WEB_DEV_SERVER, "Idempotency-Key": str(uuid.uuid4())},
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers["access-control-allow-origin"], WEB_DEV_SERVER)
        exposed = response.headers["access-control-expose-headers"].lower()
        for header in ("location", "idempotent-replayed", "x-correlation-id"):
            self.assertIn(header, exposed)

    def test_unknown_origin_gets_no_cors_headers_on_actual_request(self) -> None:
        response = self.client.get("/health/live", headers={"Origin": OTHER_ORIGIN})

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_without_configured_origins_cross_origin_calls_are_denied(self) -> None:
        response = preflight(client(None), WEB_DEV_SERVER)

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("access-control-allow-origin", response.headers)


if __name__ == "__main__":
    unittest.main()
