"""BFF móvil, HU004: cierre de sesión con el refresh token en el cuerpo (CA5-CA7)."""

import json
import unittest
import uuid

import httpx
from test_sessions import SESSION, client_with

TOKEN = str(SESSION["refresh_token"])


class RevokeBffTest(unittest.TestCase):
    def test_revocation_is_forwarded_and_returns_204(self) -> None:
        """CA5: el BFF reenvía la credencial al servicio y no la devuelve."""
        client, upstream = client_with(lambda _: httpx.Response(204))

        response = client.post("/v1/sessions/revoke", json={"refresh_token": TOKEN})

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        [sent] = upstream.requests
        self.assertEqual(sent.url.path, "/v1/sessions/revoke")
        self.assertEqual(json.loads(sent.content), {"refresh_token": TOKEN})

    def test_optional_idempotency_key_is_forwarded(self) -> None:
        """ADR-026: la cola offline reenvía la misma clave en cada reintento."""
        client, upstream = client_with(lambda _: httpx.Response(204))
        retry_id = str(uuid.uuid4())

        with_key = client.post(
            "/v1/sessions/revoke",
            json={"refresh_token": TOKEN},
            headers={"Idempotency-Key": retry_id},
        )
        without_key = client.post("/v1/sessions/revoke", json={"refresh_token": TOKEN})
        invalid = client.post(
            "/v1/sessions/revoke",
            json={"refresh_token": TOKEN},
            headers={"Idempotency-Key": "no-es-uuid"},
        )

        self.assertEqual((with_key.status_code, without_key.status_code), (204, 204))
        self.assertEqual(upstream.requests[0].headers["Idempotency-Key"], retry_id)
        self.assertNotIn("Idempotency-Key", upstream.requests[1].headers)
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(len(upstream.requests), 2)

    def test_repeating_is_idempotent(self) -> None:
        """CA7: el servicio responde 204 también si la sesión ya estaba cerrada."""
        client, upstream = client_with(lambda _: httpx.Response(204))

        responses = [
            client.post("/v1/sessions/revoke", json={"refresh_token": TOKEN}) for _ in range(2)
        ]

        self.assertEqual([r.status_code for r in responses], [204, 204])
        self.assertEqual(len(upstream.requests), 2)

    def test_invalid_body_is_rejected_in_the_bff(self) -> None:
        client, upstream = client_with(lambda _: httpx.Response(204))

        for body in ({}, {"refresh_token": "corto"}, {"refresh_token": TOKEN, "extra": 1}):
            with self.subTest(body=list(body)):
                response = client.post("/v1/sessions/revoke", json=body)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(TOKEN, response.text)
        self.assertEqual(upstream.requests, [])

    def test_service_validation_error_passes_through(self) -> None:
        client, _ = client_with(
            lambda _: httpx.Response(422, json={"code": "validation_error", "message": "m"})
        )

        response = client.post("/v1/sessions/revoke", json={"refresh_token": TOKEN})

        self.assertEqual((response.status_code, response.json()["code"]), (422, "validation_error"))

    def test_unconfirmed_revocation_is_reported(self) -> None:
        """CA6: el cliente distingue que el cierre remoto no se confirmó."""

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
                client, _ = client_with(handler)
                response = client.post("/v1/sessions/revoke", json={"refresh_token": TOKEN})
                self.assertEqual((response.status_code, response.json()["code"]), (status, code))
                self.assertNotIn(TOKEN, response.text)


if __name__ == "__main__":
    unittest.main()
