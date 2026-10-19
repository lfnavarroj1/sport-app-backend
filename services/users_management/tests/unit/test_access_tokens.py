"""Emisión de access tokens EdDSA y carga de la clave de firma (HU003 CA1 y CA5)."""

import os
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from sportapp_platform.config import ConfigurationError, load_settings
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.tokens import AccessTokenVerifier, InvalidAccessToken
from users_management.infrastructure.security.access_tokens import (
    Ed25519AccessTokenIssuer,
    load_signing_key,
)
from users_management.settings import UsersManagementSettings

ISSUER = "sportapp-users-management"
AUDIENCE = "sportapp"
SETTINGS_BASE = {
    "APP_ENV": "test",
    "DATABASE_WRITE_URL": "postgresql+psycopg://u@127.0.0.1:1/w",
    "DATABASE_READ_URL": "postgresql+psycopg://u@127.0.0.1:1/r",
    "REGISTRATION_POLICIES_VERSION": "2026-10",
    "HEALTH_DATA_CONSENT_VERSION": "health-test.v1",
}


class Ed25519AccessTokenIssuerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.issuer = Ed25519AccessTokenIssuer(
            Ed25519PrivateKey.generate(),
            issuer=ISSUER,
            audience=AUDIENCE,
            ttl=timedelta(minutes=15),
        )
        self.verifier = AccessTokenVerifier(self.issuer.jwks(), issuer=ISSUER, audience=AUDIENCE)

    def test_token_carries_identity_and_verifiable_expiry(self) -> None:
        user_id = uuid.uuid4()
        now = datetime.now(UTC)

        issued = self.issuer.issue(user_id, "organizer", now)

        verified = self.verifier.verify(issued.token)
        self.assertEqual(verified.subject, str(user_id))
        self.assertEqual(verified.actor_type, "organizer")
        self.assertEqual(verified.expires_at, issued.expires_at)
        self.assertEqual(issued.expires_at, datetime.fromtimestamp(int(now.timestamp()) + 900, UTC))

    def test_header_and_claims_follow_adr009(self) -> None:
        token = self.issuer.issue(uuid.uuid4(), "athlete", datetime.now(UTC)).token

        header = jwt.get_unverified_header(token)
        claims = jwt.decode(token, options={"verify_signature": False})
        self.assertEqual(header["alg"], "EdDSA")
        self.assertEqual(header["kid"], self.issuer.jwks()["keys"][0]["kid"])
        self.assertEqual(
            set(claims), {"iss", "aud", "sub", "iat", "nbf", "exp", "jti", "actor_type"}
        )
        self.assertEqual(claims["nbf"], claims["iat"])

    def test_each_token_has_a_unique_id(self) -> None:
        now = datetime.now(UTC)
        user_id = uuid.uuid4()

        first, second = (self.issuer.issue(user_id, "athlete", now) for _ in range(2))

        self.assertNotEqual(
            self.verifier.verify(first.token).token_id, self.verifier.verify(second.token).token_id
        )

    def test_expired_token_is_rejected_by_other_services(self) -> None:
        old = datetime.now(UTC) - timedelta(minutes=16)

        with self.assertRaises(InvalidAccessToken):
            self.verifier.verify(self.issuer.issue(uuid.uuid4(), "athlete", old).token)

    def test_jwks_exposes_only_public_material(self) -> None:
        [key] = self.issuer.jwks()["keys"]

        self.assertEqual(set(key), {"kty", "crv", "x", "kid", "alg", "use"})


class SigningKeySettingsTest(unittest.TestCase):
    def load(self, **extra: str) -> UsersManagementSettings:
        with mock.patch.dict(os.environ, {**SETTINGS_BASE, **extra}, clear=True):
            return load_settings(UsersManagementSettings)

    def test_accepts_pem_with_real_or_escaped_line_breaks(self) -> None:
        pem = ed25519_private_key_pem()

        for value in (pem, pem.replace("\n", "\\n")):
            with self.subTest(escaped="\\n" in value):
                settings = self.load(JWT_PRIVATE_KEY=value)
                self.assertIsInstance(settings.signing_key(), Ed25519PrivateKey)

    def test_defaults_match_the_approved_policy(self) -> None:
        settings = self.load(JWT_PRIVATE_KEY=ed25519_private_key_pem())

        self.assertEqual(settings.access_token_ttl, timedelta(minutes=15))
        self.assertEqual(settings.refresh_token_ttl, timedelta(days=7))
        self.assertEqual(settings.login_max_failures, 5)
        self.assertEqual(settings.login_failure_window, timedelta(minutes=15))

    def test_missing_or_invalid_key_stops_startup_without_revealing_it(self) -> None:
        ec_key = (
            generate_private_key(SECP256R1())
            .private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
            .decode()
        )
        for extra in ({}, {"JWT_PRIVATE_KEY": "CAMBIAR"}, {"JWT_PRIVATE_KEY": ec_key}):
            with self.subTest(case=list(extra)), self.assertRaises(ConfigurationError) as error:
                self.load(**extra)
            self.assertIn("JWT_PRIVATE_KEY", str(error.exception))
            self.assertNotIn("PRIVATE KEY-----", str(error.exception))

    def test_load_signing_key_rejects_non_ed25519(self) -> None:
        with self.assertRaises(ValueError):
            load_signing_key("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----")


if __name__ == "__main__":
    unittest.main()
