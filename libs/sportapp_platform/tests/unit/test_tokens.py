"""HU003 CA5: un servicio valida firma, emisor, audiencia y vigencia del access token."""

import base64
import json
import time
import unittest
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from sportapp_platform.tokens import (
    AccessTokenVerifier,
    InvalidAccessToken,
    public_jwk,
)

ISSUER = "sportapp-users-management"
AUDIENCE = "sportapp"
SUBJECT = "6f1c1d7e-3b8a-4c1e-9a55-0d6c2f0b7a11"


def claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    values: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": SUBJECT,
        "iat": now,
        "nbf": now,
        "exp": now + 900,
        "jti": "a5b7c0d2-0000-4000-8000-000000000001",
        "actor_type": "athlete",
    }
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not None}


class PublicJwkTest(unittest.TestCase):
    def test_kid_is_rfc7638_thumbprint(self) -> None:
        # Vector de RFC 8037, apéndice A.3.
        x = "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"
        public_key = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(x + "="))

        jwk = public_jwk(public_key)

        self.assertEqual(jwk["x"], x)
        self.assertEqual(jwk["kid"], "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k")
        self.assertEqual(
            {key: jwk[key] for key in ("kty", "crv", "alg", "use")},
            {"kty": "OKP", "crv": "Ed25519", "alg": "EdDSA", "use": "sig"},
        )
        self.assertNotIn("d", jwk)


class AccessTokenVerifierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        self.jwk = public_jwk(self.key.public_key())
        self.verifier = AccessTokenVerifier({"keys": [self.jwk]}, issuer=ISSUER, audience=AUDIENCE)

    def sign(
        self, payload: dict[str, Any], signer: Ed25519PrivateKey | None = None, **header: Any
    ) -> str:
        headers = {"kid": self.jwk["kid"], **header}
        return jwt.encode(payload, signer or self.key, algorithm="EdDSA", headers=headers)

    def test_accepts_current_token_signed_by_published_key(self) -> None:
        verified = self.verifier.verify(self.sign(claims()))

        self.assertEqual(verified.subject, SUBJECT)
        self.assertEqual(verified.actor_type, "athlete")
        self.assertEqual(verified.token_id, "a5b7c0d2-0000-4000-8000-000000000001")

    def test_rejects_expired_or_not_yet_valid_tokens(self) -> None:
        now = int(time.time())
        for payload in (
            claims(exp=now - 1),
            claims(nbf=now + 120, iat=now),
        ):
            with self.subTest(payload=payload), self.assertRaises(InvalidAccessToken):
                self.verifier.verify(self.sign(payload))

    def test_rejects_wrong_issuer_or_audience(self) -> None:
        for payload in (claims(iss="otro-emisor"), claims(aud="otra-audiencia")):
            with self.subTest(payload=payload), self.assertRaises(InvalidAccessToken):
                self.verifier.verify(self.sign(payload))

    def test_rejects_tampered_payload(self) -> None:
        header, _, signature = self.sign(claims()).split(".")
        forged = (
            base64.urlsafe_b64encode(
                json.dumps(claims(sub="00000000-0000-0000-0000-000000000000")).encode()
            )
            .rstrip(b"=")
            .decode()
        )

        with self.assertRaises(InvalidAccessToken):
            self.verifier.verify(f"{header}.{forged}.{signature}")

    def test_rejects_token_signed_by_unpublished_key(self) -> None:
        with self.assertRaises(InvalidAccessToken):
            self.verifier.verify(self.sign(claims(), signer=Ed25519PrivateKey.generate()))

    def test_rejects_unknown_kid_and_other_algorithms(self) -> None:
        unsigned = jwt.encode(
            claims(), key=None, algorithm="none", headers={"kid": self.jwk["kid"]}
        )
        symmetric = jwt.encode(
            claims(), "x" * 32, algorithm="HS256", headers={"kid": self.jwk["kid"]}
        )
        for token in (self.sign(claims(), kid="desconocido"), unsigned, symmetric, "no-es-jwt"):
            with self.subTest(token=token[:20]), self.assertRaises(InvalidAccessToken):
                self.verifier.verify(token)

    def test_rejects_tokens_missing_required_claims(self) -> None:
        for missing in ("sub", "jti", "exp", "nbf", "iat"):
            with self.subTest(missing=missing), self.assertRaises(InvalidAccessToken):
                self.verifier.verify(self.sign(claims(**{missing: None})))

    def test_requires_at_least_one_ed25519_key(self) -> None:
        with self.assertRaises(ValueError):
            AccessTokenVerifier(
                {"keys": [{"kty": "RSA", "kid": "x"}]}, issuer=ISSUER, audience=AUDIENCE
            )


if __name__ == "__main__":
    unittest.main()
