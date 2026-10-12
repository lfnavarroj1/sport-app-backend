"""BFF web, HU003 (contrato 0.4.0): refresh token en cookie HttpOnly y CSRF por `Origin`."""

import json
import unittest
from typing import Any

import httpx
from fastapi.testclient import TestClient

from bff_web.main import create_app
from bff_web.settings import BffWebSettings
from sportapp_platform.correlation import CORRELATION_HEADER

ORIGIN = "http://localhost:4200"
COOKIE = "__Secure-sportapp_refresh"
PASSWORD = "contraseña-muy-secreta"
REFRESH = "r" * 43
ROTATED = "n" * 43
SESSION = {
    "token_type": "Bearer",
    "access_token": "eyJ.access.token",
    "expires_in": 900,
    "refresh_token": REFRESH,
    "refresh_expires_in": 604800,
    "user": {
        "user_id": "6f1c1d7e-3b8a-4c1e-9a55-0d6c2f0b7a11",
        "full_name": "Ana Pérez",
        "email": "ana@example.com",
        "actor_type": "athlete",
    },
}


def settings(**overrides: Any) -> BffWebSettings:
    values: dict[str, Any] = {
        "app_env": "test",
        "users_management_base_url": "http://users.invalid",
        "affiliation_management_base_url": "http://affiliation.invalid",
        "training_management_base_url": "http://training.invalid",
        "cors_allowed_origins": (ORIGIN,),
    }
    values.update(overrides)
    return BffWebSettings(**values)


class UpstreamDouble:
    def __init__(self, handler: Any) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result: httpx.Response = self._handler(request)
        return result


def client_with(
    handler: Any, origin: str | None = ORIGIN, **overrides: Any
) -> tuple[TestClient, UpstreamDouble]:
    upstream = UpstreamDouble(handler)
    http = httpx.Client(base_url="http://users.invalid", transport=httpx.MockTransport(upstream))
    # HTTPS: el cliente de pruebas solo reenvía cookies `Secure` por un origen seguro.
    app = create_app(settings(**overrides), users_http=http)
    headers = {"Origin": origin} if origin else {}
    return TestClient(app, base_url="https://testserver", headers=headers), upstream


def session_ok(request: httpx.Request) -> httpx.Response:
    token = ROTATED if request.url.path.endswith("/refresh") else REFRESH
    return httpx.Response(
        200, json=SESSION | {"refresh_token": token}, headers={"Cache-Control": "no-store"}
    )


def login(client: TestClient, **headers: str) -> httpx.Response:
    return client.post(
        "/v1/sessions", json={"email": "ana@example.com", "password": PASSWORD}, headers=headers
    )


def set_cookie_header(response: httpx.Response) -> str:
    return response.headers.get("set-cookie", "")


class LoginCookieTest(unittest.TestCase):
    def test_login_sets_httponly_cookie_and_removes_the_token_from_the_body(self) -> None:
        """CA1 en web: el refresh token nunca queda al alcance del JavaScript."""
        client, upstream = client_with(session_ok)

        response = login(client, **{CORRELATION_HEADER: "1b4e28ba-2fa1-4d3b-a3f5-ef19b5a7633b"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertNotIn("refresh_token", body)
        self.assertNotIn(REFRESH, response.text)
        self.assertEqual(body["refresh_expires_in"], 604800)
        self.assertEqual(body["access_token"], "eyJ.access.token")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        cookie = set_cookie_header(response)
        self.assertTrue(cookie.startswith(f"{COOKIE}={REFRESH};"), cookie)
        for attribute in ("HttpOnly", "Secure", "Path=/v1/sessions", "Max-Age=604800"):
            self.assertIn(attribute, cookie)
        self.assertIn("samesite=strict", cookie.lower())
        [sent] = upstream.requests
        self.assertEqual(sent.headers["X-Client-IP"], "testclient")
        self.assertEqual(
            json.loads(sent.content), {"email": "ana@example.com", "password": PASSWORD}
        )

    def test_same_site_is_configurable(self) -> None:
        client, _ = client_with(session_ok, refresh_cookie_same_site="none")

        cookie = set_cookie_header(login(client))

        self.assertIn("samesite=none", cookie.lower())
        self.assertIn("Secure", cookie)

    def test_service_errors_pass_through_without_cookie(self) -> None:
        """CA2 a CA4."""
        cases = [
            (401, {"code": "invalid_credentials"}, {}),
            (429, {"code": "too_many_attempts"}, {"Retry-After": "120"}),
        ]
        for status, body, headers in cases:
            with self.subTest(status=status):
                client, _ = client_with(
                    lambda _r, s=status, b=body, h=headers: httpx.Response(s, json=b, headers=h)
                )

                response = login(client)

                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json(), body)
                self.assertEqual(set_cookie_header(response), "")
                for name, value in headers.items():
                    self.assertEqual(response.headers[name], value)

    def test_unexpected_upstream_body_without_token_is_503(self) -> None:
        client, _ = client_with(
            lambda _r: httpx.Response(
                200, json={k: v for k, v in SESSION.items() if k != "refresh_token"}
            )
        )

        response = login(client)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "upstream_unavailable")


class RefreshCookieTest(unittest.TestCase):
    def test_refresh_reads_the_cookie_rotates_it_and_returns_a_new_access_token(self) -> None:
        client, upstream = client_with(session_ok)
        login(client)

        response = client.post("/v1/sessions/refresh")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("refresh_token", response.json())
        self.assertTrue(set_cookie_header(response).startswith(f"{COOKIE}={ROTATED};"))
        refresh_call = upstream.requests[-1]
        self.assertEqual(refresh_call.url.path, "/v1/sessions/refresh")
        self.assertEqual(json.loads(refresh_call.content), {"refresh_token": REFRESH})
        self.assertNotIn("X-Client-IP", refresh_call.headers)

    def test_invalid_session_from_the_service_clears_the_cookie(self) -> None:
        for status in (401, 422):
            with self.subTest(status=status):
                client, _ = client_with(
                    lambda _r, s=status: httpx.Response(s, json={"code": "invalid_refresh_token"})
                )

                response = client.post(
                    "/v1/sessions/refresh", headers={"Cookie": f"{COOKIE}={REFRESH}"}
                )

                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["code"], "invalid_refresh_token")
                cleared = set_cookie_header(response)
                self.assertTrue(cleared.startswith(f'{COOKIE}="";'), cleared)
                self.assertIn("Max-Age=0", cleared)
                self.assertIn("Path=/v1/sessions", cleared)

    def test_missing_or_malformed_cookie_is_rejected_without_calling_the_service(self) -> None:
        client, upstream = client_with(session_ok)

        missing = client.post("/v1/sessions/refresh")
        malformed = client.post("/v1/sessions/refresh", headers={"Cookie": f"{COOKIE}=corto"})

        for response in (missing, malformed):
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["code"], "invalid_refresh_token")
            self.assertIn("Max-Age=0", set_cookie_header(response))
        self.assertEqual(upstream.requests, [])

    def test_upstream_failures_keep_the_cookie(self) -> None:
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

                response = client.post(
                    "/v1/sessions/refresh", headers={"Cookie": f"{COOKIE}={REFRESH}"}
                )

                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["code"], code)
                self.assertEqual(
                    set_cookie_header(response), "", "un fallo temporal no cierra sesión"
                )


class CsrfOriginTest(unittest.TestCase):
    def test_session_operations_require_an_allowed_origin(self) -> None:
        for name, origin in (("otro origen", "https://evil.example"), ("sin origen", None)):
            with self.subTest(name=name):
                client, upstream = client_with(session_ok, origin=origin)

                responses = [
                    login(client),
                    client.post("/v1/sessions/refresh", headers={"Cookie": f"{COOKIE}={REFRESH}"}),
                ]

                for response in responses:
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json()["code"], "origin_not_allowed")
                    self.assertEqual(set_cookie_header(response), "")
                self.assertEqual(upstream.requests, [])

    def test_without_configured_origins_no_session_can_be_opened(self) -> None:
        client, upstream = client_with(session_ok, cors_allowed_origins=())

        response = login(client)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(upstream.requests, [])

    def test_transport_validation_still_applies(self) -> None:
        client, upstream = client_with(session_ok)

        responses = [
            client.post("/v1/sessions", json={"email": "ana@example.com"}),
            client.post("/v1/sessions", json={"email": "a@b.co", "password": PASSWORD, "x": 1}),
        ]

        self.assertEqual([r.status_code for r in responses], [422, 422])
        self.assertEqual(upstream.requests, [])
        self.assertNotIn(PASSWORD, "".join(r.text for r in responses))


class CorsCredentialsTest(unittest.TestCase):
    def test_browser_may_send_the_cookie_and_read_retry_after(self) -> None:
        client, _ = client_with(
            lambda _r: httpx.Response(
                429, json={"code": "too_many_attempts"}, headers={"Retry-After": "60"}
            )
        )

        preflight = client.options(
            "/v1/sessions/refresh",
            headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"},
        )
        response = login(client)

        self.assertEqual(preflight.headers["access-control-allow-credentials"], "true")
        self.assertEqual(preflight.headers["access-control-allow-origin"], ORIGIN)
        self.assertEqual(response.headers["access-control-allow-credentials"], "true")
        self.assertIn("retry-after", response.headers["access-control-expose-headers"].lower())


if __name__ == "__main__":
    unittest.main()
