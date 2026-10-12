"""BFF web, HU004: cierre de sesión con la cookie `HttpOnly` del refresh token."""

import json
import unittest

import httpx
from test_sessions import COOKIE, ORIGIN, REFRESH, client_with, login, session_ok, set_cookie_header


def revoked(_: httpx.Request) -> httpx.Response:
    return httpx.Response(204)


def logged_in(handler: object, origin: str | None = ORIGIN) -> tuple[object, object]:
    """Inicia sesión (deja la cookie en el cliente) y luego responde con `handler`."""

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions":
            return session_ok(request)
        return handler(request)  # type: ignore[operator]

    client, upstream = client_with(route, origin=origin)
    login(client)
    return client, upstream


class RevokeCookieTest(unittest.TestCase):
    def test_revokes_the_cookie_session_and_clears_it(self) -> None:
        """CA1 y CA4 en web: el servicio recibe el token de la cookie; el navegador lo pierde."""
        client, upstream = logged_in(revoked)

        response = client.post("/v1/sessions/revoke")  # type: ignore[attr-defined]

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        cleared = set_cookie_header(response)
        self.assertTrue(cleared.startswith(f"{COOKIE}="), cleared)
        self.assertIn("Max-Age=0", cleared)
        self.assertIn("Path=/v1/sessions", cleared)
        sent = upstream.requests[-1]  # type: ignore[attr-defined]
        self.assertEqual(sent.url.path, "/v1/sessions/revoke")
        self.assertEqual(json.loads(sent.content), {"refresh_token": REFRESH})
        self.assertNotIn(REFRESH, response.text)
        self.assertNotIn(COOKIE, client.cookies)  # type: ignore[attr-defined]

    def test_without_cookie_is_204_without_calling_the_service(self) -> None:
        """CA2: repetir el cierre (ya sin cookie) es seguro."""
        client, upstream = client_with(revoked)

        first = client.post("/v1/sessions/revoke")
        second = client.post("/v1/sessions/revoke")

        self.assertEqual((first.status_code, second.status_code), (204, 204))
        self.assertEqual(upstream.requests, [])
        self.assertIn("Max-Age=0", set_cookie_header(first))

    def test_malformed_cookie_is_closed_locally(self) -> None:
        client, upstream = client_with(revoked)
        client.cookies.set(COOKIE, "corto", domain="testserver", path="/v1/sessions")

        response = client.post("/v1/sessions/revoke")

        self.assertEqual(response.status_code, 204)
        self.assertEqual(upstream.requests, [])

    def test_service_422_is_treated_as_nothing_to_revoke(self) -> None:
        client, _ = logged_in(lambda _: httpx.Response(422, json={"code": "validation_error"}))

        response = client.post("/v1/sessions/revoke")  # type: ignore[attr-defined]

        self.assertEqual(response.status_code, 204)
        self.assertIn("Max-Age=0", set_cookie_header(response))

    def test_foreign_origin_cannot_close_the_session(self) -> None:
        """CSRF: otro sitio no cierra la sesión ni borra la cookie."""
        client, upstream = logged_in(revoked)
        calls = len(upstream.requests)  # type: ignore[attr-defined]

        for headers in ({"Origin": "https://evil.example"}, {"Origin": ""}):
            with self.subTest(headers=headers):
                response = client.post("/v1/sessions/revoke", headers=headers)  # type: ignore[attr-defined]
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["code"], "origin_not_allowed")
                self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(len(upstream.requests), calls)  # type: ignore[attr-defined]

    def test_unavailable_service_still_clears_the_cookie(self) -> None:
        """Decisión del 2026-10-12: cierre local garantizado aunque no se confirme el remoto."""

        def down(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("usuarios caído")

        def slow(_: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("usuarios lento")

        for handler, status, code in (
            (down, 503, "upstream_unavailable"),
            (slow, 504, "upstream_timeout"),
            (lambda _: httpx.Response(500), 503, "upstream_unavailable"),
        ):
            with self.subTest(status=status):
                client, _ = logged_in(handler)
                response = client.post("/v1/sessions/revoke")  # type: ignore[attr-defined]
                self.assertEqual((response.status_code, response.json()["code"]), (status, code))
                self.assertIn("Max-Age=0", set_cookie_header(response))
                self.assertNotIn(COOKIE, client.cookies)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
