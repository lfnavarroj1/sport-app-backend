"""Access tokens JWT firmados con Ed25519 (EdDSA), aprobado el 2026-10-10 (ADR-009).

Claims: `iss`, `aud`, `sub` (user_id), `iat`, `nbf`, `exp`, `jti` y `actor_type`.
El encabezado lleva `kid` = huella RFC 7638 de la clave pública, publicada en
`/.well-known/jwks.json` para que los demás servicios validen sin secreto compartido.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from sportapp_platform.tokens import SIGNING_ALGORITHM, public_jwk
from users_management.application.ports import IssuedAccessToken


def normalize_private_key_pem(value: str) -> str:
    """Admite el PEM en una sola línea con `\\n` literales (archivos `.env`)."""
    return value.replace("\\n", "\n").strip()


def load_signing_key(pem: str) -> Ed25519PrivateKey:
    try:
        key = load_pem_private_key(normalize_private_key_pem(pem).encode("ascii"), password=None)
    except (ValueError, TypeError, UnicodeEncodeError, UnsupportedAlgorithm):
        raise ValueError("debe ser una clave privada Ed25519 en formato PEM") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("debe ser una clave privada Ed25519 en formato PEM")
    return key


class Ed25519AccessTokenIssuer:
    def __init__(
        self,
        private_key: Ed25519PrivateKey,
        *,
        issuer: str,
        audience: str,
        ttl: timedelta,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._private_key = private_key
        self._issuer = issuer
        self._audience = audience
        self._ttl = ttl
        self._new_id = new_id
        self._jwk = public_jwk(private_key.public_key())

    def issue(self, user_id: UUID, actor_type: str, now: datetime) -> IssuedAccessToken:
        issued_at = int(now.timestamp())
        expires_at = issued_at + int(self._ttl.total_seconds())
        claims: dict[str, Any] = {
            "iss": self._issuer,
            "aud": self._audience,
            "sub": str(user_id),
            "iat": issued_at,
            "nbf": issued_at,
            "exp": expires_at,
            "jti": str(self._new_id()),
            "actor_type": actor_type,
        }
        token = jwt.encode(
            claims,
            self._private_key,
            algorithm=SIGNING_ALGORITHM,
            headers={"kid": self._jwk["kid"], "typ": "JWT"},
        )
        return IssuedAccessToken(token=token, expires_at=datetime.fromtimestamp(expires_at, UTC))

    def jwks(self) -> dict[str, list[dict[str, str]]]:
        return {"keys": [dict(self._jwk)]}
