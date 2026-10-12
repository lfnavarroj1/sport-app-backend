"""`CachedJwksVerifier`: JWKS remoto en memoria, rotación de clave y fallos de obtención."""

import time
import unittest
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sportapp_platform.tokens import (
    CachedJwksVerifier,
    InvalidAccessToken,
    JwksUnavailable,
    public_jwk,
)

ISSUER = "sportapp-users-management"
AUDIENCE = "sportapp"


def sign(key: Ed25519PrivateKey, **claims: Any) -> str:
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "6f1c1d7e-3b8a-4c1e-9a55-0d6c2f0b7a11",
        "iat": now,
        "nbf": now,
        "exp": now + 900,
        "jti": "j-1",
        "actor_type": "athlete",
        **claims,
    }
    return jwt.encode(
        payload, key, algorithm="EdDSA", headers={"kid": public_jwk(key.public_key())["kid"]}
    )


class FakeJwksSource:
    def __init__(self, *keys: Ed25519PrivateKey) -> None:
        self.keys = list(keys)
        self.calls = 0
        self.fail = False

    def __call__(self) -> dict[str, Any]:
        self.calls += 1
        if self.fail:
            raise ConnectionError("users-management caído")
        return {"keys": [public_jwk(key.public_key()) for key in self.keys]}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class CachedJwksVerifierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        self.source = FakeJwksSource(self.key)
        self.clock = FakeClock()
        self.verifier = CachedJwksVerifier(
            self.source, issuer=ISSUER, audience=AUDIENCE, clock=self.clock
        )

    def test_fetches_once_and_reuses_the_keys(self) -> None:
        for _ in range(5):
            self.assertEqual(self.verifier.verify(sign(self.key)).actor_type, "athlete")

        self.assertEqual(self.source.calls, 1)

    def test_refreshes_after_the_ttl(self) -> None:
        self.verifier.verify(sign(self.key))
        self.clock.now += 301

        self.verifier.verify(sign(self.key))

        self.assertEqual(self.source.calls, 2)

    def test_rotated_key_triggers_a_refresh(self) -> None:
        self.verifier.verify(sign(self.key))
        self.clock.now += 31
        rotated = Ed25519PrivateKey.generate()
        self.source.keys.append(rotated)

        self.assertEqual(self.verifier.verify(sign(rotated)).subject.count("-"), 4)
        self.assertEqual(self.source.calls, 2)

    def test_invented_kids_cannot_force_repeated_downloads(self) -> None:
        self.verifier.verify(sign(self.key))
        stranger = Ed25519PrivateKey.generate()

        for _ in range(5):
            with self.assertRaises(InvalidAccessToken):
                self.verifier.verify(sign(stranger))

        self.assertEqual(self.source.calls, 1, "dentro del intervalo mínimo no se recarga")

    def test_unavailable_jwks_without_previous_keys_is_not_an_invalid_token(self) -> None:
        self.source.fail = True

        with self.assertRaises(JwksUnavailable):
            self.verifier.verify(sign(self.key))

    def test_failed_refresh_keeps_the_previous_keys(self) -> None:
        self.verifier.verify(sign(self.key))
        self.source.fail = True
        self.clock.now += 301

        self.assertEqual(self.verifier.verify(sign(self.key)).actor_type, "athlete")
        self.verifier.verify(sign(self.key))
        self.assertEqual(self.source.calls, 2, "el reintento espera el intervalo mínimo")

    def test_tokens_still_must_be_valid(self) -> None:
        with self.assertRaises(InvalidAccessToken):
            self.verifier.verify(sign(self.key, exp=int(time.time()) - 1))
        with self.assertRaises(InvalidAccessToken):
            self.verifier.verify("no-es-jwt")


if __name__ == "__main__":
    unittest.main()
