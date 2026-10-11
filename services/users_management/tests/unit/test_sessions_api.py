"""Contrato HTTP de HU003 en `users_management` con los casos de uso reales en memoria."""

import unittest
from datetime import timedelta
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fakes import (
    FakeTokenIssuer,
    FakeVerifier,
    FixedClock,
    InMemoryAuthStore,
    account,
    login_with,
    refresh_with,
)
from fastapi.testclient import TestClient

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from sportapp_platform.correlation import CORRELATION_HEADER
from users_management.api.sessions import build_jwks_router, build_sessions_router
from users_management.application.commands.login import throttle_key
from users_management.domain.account.model import AccountStatus
from users_management.infrastructure.security.access_tokens import Ed25519AccessTokenIssuer

PASSWORD = "contraseña-muy-secreta"
SESSION_FIELDS = {
    "token_type",
    "access_token",
    "expires_in",
    "refresh_token",
    "refresh_expires_in",
    "user",
}


class SessionsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryAuthStore()
        self.user = self.store.add_account(account())
        self.store.add_account(account(email="baja@example.com", status=AccountStatus.DISABLED))
        self.clock = FixedClock()
        self.issuer = FakeTokenIssuer()
        jwks_issuer = Ed25519AccessTokenIssuer(
            Ed25519PrivateKey.generate(), issuer="i", audience="a", ttl=timedelta(minutes=15)
        )
        app = create_service_app(
            service_name="users-management",
            title="t",
            api_version="0",
            settings=ServiceSettings(app_env="test"),  # type: ignore[call-arg]
            routers=[
                build_sessions_router(
                    login_with(self.store, FakeVerifier(), self.issuer, self.clock),
                    refresh_with(self.store, self.issuer, self.clock),
                ),
                build_jwks_router(jwks_issuer.jwks()),
            ],
        )
        self.client = TestClient(app)

    def login(
        self, email: str = "ana@example.com", password: str = PASSWORD, **headers: str
    ) -> Any:
        return self.client.post(
            "/v1/sessions", json={"email": email, "password": password}, headers=headers
        )

    def test_200_returns_identity_and_tokens_without_caching(self) -> None:
        """CA1."""
        response = self.login()

        data = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(set(data), SESSION_FIELDS)
        self.assertEqual(data["token_type"], "Bearer")
        self.assertEqual(data["expires_in"], 900)
        self.assertEqual(data["refresh_expires_in"], 604800)
        self.assertEqual(
            data["user"],
            {
                "user_id": str(self.user.user_id),
                "full_name": "Ana Pérez",
                "email": "ana@example.com",
                "actor_type": "athlete",
            },
        )
        self.assertNotIn(PASSWORD, response.text)

    def test_wrong_password_unknown_email_and_disabled_account_get_the_same_401(self) -> None:
        """CA2 y CA3: el cuerpo no permite distinguir los casos."""
        responses = [
            self.login(password="incorrecta"),
            self.login(email="nadie@example.com"),
            self.login(email="baja@example.com"),
        ]

        bodies = []
        for response in responses:
            self.assertEqual(response.status_code, 401)
            body = response.json()
            self.assertEqual(body["correlation_id"], response.headers[CORRELATION_HEADER])
            bodies.append({key: body[key] for key in ("code", "message", "details")})
            self.assertNotIn("access_token", response.text)
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(bodies[0], bodies[2])
        self.assertEqual(bodies[0]["code"], "invalid_credentials")
        self.assertEqual(self.issuer.issued, [])

    def test_429_with_retry_after_once_the_policy_is_exceeded(self) -> None:
        """CA4."""
        for _ in range(5):
            self.login(password="incorrecta")

        response = self.login()

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "too_many_attempts")
        self.assertEqual(response.headers["Retry-After"], "900")
        self.assertNotIn("access_token", response.text)

    def test_client_ip_from_the_bff_feeds_the_throttle(self) -> None:
        self.login(password="incorrecta", **{"X-Client-IP": "203.0.113.7"})
        self.login(password="incorrecta", **{"X-Client-IP": "no-es-ip"})

        self.assertIn(throttle_key("ana@example.com", "203.0.113.7"), self.store.failures)
        # Una cabecera inválida se ignora y se usa la IP del par de la conexión.
        self.assertIn(throttle_key("ana@example.com", "testclient"), self.store.failures)

    def test_invalid_body_is_422_without_echoing_the_password(self) -> None:
        missing = self.client.post("/v1/sessions", json={"email": "ana@example.com"})
        extra = self.client.post(
            "/v1/sessions",
            json={"email": "ana@example.com", "password": PASSWORD, "role": "admin"},
        )
        too_long = self.login(password="x" * 129)

        for response in (missing, extra, too_long):
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["code"], "validation_error")
            self.assertNotIn(PASSWORD, response.text)

    def test_refresh_rotates_and_rejects_reuse(self) -> None:
        first = self.login().json()

        renewed = self.client.post(
            "/v1/sessions/refresh", json={"refresh_token": first["refresh_token"]}
        )
        reused = self.client.post(
            "/v1/sessions/refresh", json={"refresh_token": first["refresh_token"]}
        )

        self.assertEqual(renewed.status_code, 200)
        self.assertEqual(renewed.headers["Cache-Control"], "no-store")
        self.assertEqual(set(renewed.json()), SESSION_FIELDS)
        self.assertNotEqual(renewed.json()["refresh_token"], first["refresh_token"])
        self.assertEqual(reused.status_code, 401)
        self.assertEqual(reused.json()["code"], "invalid_refresh_token")

    def test_refresh_with_unknown_or_malformed_token(self) -> None:
        unknown = self.client.post("/v1/sessions/refresh", json={"refresh_token": "x" * 43})
        short = self.client.post("/v1/sessions/refresh", json={"refresh_token": "corto"})

        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(short.status_code, 422)

    def test_jwks_publishes_the_public_key_for_other_services(self) -> None:
        """CA5."""
        response = self.client.get("/.well-known/jwks.json")

        [key] = response.json()["keys"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "public, max-age=300")
        self.assertEqual((key["kty"], key["crv"], key["alg"]), ("OKP", "Ed25519", "EdDSA"))
        self.assertNotIn("d", key)


if __name__ == "__main__":
    unittest.main()
