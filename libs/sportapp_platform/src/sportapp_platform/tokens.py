"""Access tokens firmados (ADR-009): clave pública en JWK y verificación.

Capacidad técnica compartida: cualquier servicio valida aquí la firma, el
emisor, la audiencia y la vigencia de un access token emitido por
`users_management`. No contiene reglas de autorización de ningún dominio.
"""

import base64
import hashlib
import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

SIGNING_ALGORITHM = "EdDSA"
REQUIRED_CLAIMS = ("iss", "aud", "sub", "exp", "nbf", "iat", "jti")


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def public_jwk(public_key: Ed25519PublicKey) -> dict[str, str]:
    """JWK pública (RFC 8037) con `kid` igual a su huella RFC 7638."""
    x = _base64url(public_key.public_bytes(Encoding.Raw, PublicFormat.Raw))
    # La huella usa solo los miembros obligatorios, ordenados y sin espacios.
    canonical = json.dumps(
        {"crv": "Ed25519", "kty": "OKP", "x": x}, separators=(",", ":"), sort_keys=True
    )
    kid = _base64url(hashlib.sha256(canonical.encode("ascii")).digest())
    return {"kty": "OKP", "crv": "Ed25519", "x": x, "kid": kid, "alg": "EdDSA", "use": "sig"}


class InvalidAccessToken(Exception):
    """El token no es válido para este servicio; el motivo no se revela al cliente."""

    code = "invalid_access_token"


@dataclass(frozen=True)
class VerifiedAccessToken:
    subject: str
    actor_type: str | None
    token_id: str
    expires_at: datetime


class AccessTokenVerifier:
    """Acepta solo tokens EdDSA firmados por una clave del JWKS, vigentes y dirigidos a la
    audiencia esperada. Cualquier otro algoritmo (incluido `none`) se rechaza."""

    def __init__(
        self,
        jwks: Mapping[str, Any],
        *,
        issuer: str,
        audience: str,
        leeway_seconds: float = 0,
    ) -> None:
        self._keys = {
            jwk["kid"]: jwt.PyJWK(dict(jwk), algorithm=SIGNING_ALGORITHM)
            for jwk in jwks.get("keys", [])
            if jwk.get("kty") == "OKP" and jwk.get("crv") == "Ed25519" and jwk.get("kid")
        }
        if not self._keys:
            raise ValueError("El JWKS no contiene claves Ed25519 con `kid`.")
        self._issuer = issuer
        self._audience = audience
        self._leeway = leeway_seconds

    def verify(self, token: str) -> VerifiedAccessToken:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise InvalidAccessToken() from None
        kid = header.get("kid")
        key = self._keys.get(kid) if isinstance(kid, str) else None
        if key is None or header.get("alg") != SIGNING_ALGORITHM:
            raise InvalidAccessToken()
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[SIGNING_ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": list(REQUIRED_CLAIMS)},
            )
        except jwt.PyJWTError:
            raise InvalidAccessToken() from None
        actor_type = claims.get("actor_type")
        return VerifiedAccessToken(
            subject=str(claims["sub"]),
            actor_type=actor_type if isinstance(actor_type, str) else None,
            token_id=str(claims["jti"]),
            expires_at=datetime.fromtimestamp(int(claims["exp"]), UTC),
        )

    def knows(self, kid: str) -> bool:
        return kid in self._keys


class JwksUnavailable(Exception):
    """No fue posible obtener el JWKS; no se debe confundir con un token inválido."""


class CachedJwksVerifier:
    """Valida con el JWKS remoto de `users_management`, guardado en memoria.

    Lo recarga al vencer `ttl_seconds` y ante un `kid` desconocido (rotación de
    clave), como máximo una vez cada `refresh_interval_seconds` para que un `kid`
    inventado no fuerce descargas repetidas.
    """

    def __init__(
        self,
        fetch_jwks: Callable[[], Mapping[str, Any]],
        *,
        issuer: str,
        audience: str,
        ttl_seconds: float = 300,
        refresh_interval_seconds: float = 30,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch_jwks
        self._issuer = issuer
        self._audience = audience
        self._ttl = ttl_seconds
        self._refresh_interval = refresh_interval_seconds
        self._clock = clock
        self._verifier: AccessTokenVerifier | None = None
        self._fetched_at = float("-inf")
        self._lock = threading.Lock()

    def verify(self, token: str) -> VerifiedAccessToken:
        verifier = self._current(force=False)
        kid = self._unverified_kid(token)
        if kid is not None and not verifier.knows(kid):
            verifier = self._current(force=True)
        return verifier.verify(token)

    @staticmethod
    def _unverified_kid(token: str) -> str | None:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError:
            return None
        return kid if isinstance(kid, str) else None

    def _current(self, *, force: bool) -> AccessTokenVerifier:
        with self._lock:
            age = self._clock() - self._fetched_at
            stale = self._verifier is None or age >= self._ttl
            may_refresh = force and age >= self._refresh_interval
            if stale or may_refresh:
                try:
                    jwks = self._fetch()
                    verifier = AccessTokenVerifier(
                        jwks, issuer=self._issuer, audience=self._audience
                    )
                except Exception as error:
                    if self._verifier is None:
                        raise JwksUnavailable() from error
                    # Se conserva el JWKS anterior y se reintenta tras el intervalo mínimo.
                    self._fetched_at = self._clock() - self._ttl + self._refresh_interval
                else:
                    self._verifier = verifier
                    self._fetched_at = self._clock()
            if self._verifier is None:
                raise JwksUnavailable()
            return self._verifier
