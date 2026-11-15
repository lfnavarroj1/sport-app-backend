"""Cifrado en reposo de los tokens de Strava (HU031, ADR-042 §3).

AES-256-GCM con la clave `STRAVA_TOKEN_KEY` (32 bytes en base64 URL-safe, mismo formato que
`PUSH_TOKEN_ENCRYPTION_KEY` de Eventos) y un nonce aleatorio de 12 bytes antepuesto al texto
cifrado. Los datos asociados (AAD) son `user_id.bytes + b":" + b"access"|b"refresh"` (R): un
token copiado a otra fila o intercambiado entre `access` y `refresh` no se descifra. La clave
nunca se registra ni se versiona; solo este servicio la lee.
"""

import base64
import binascii
import os
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from third_party_management.application.ports import ProviderTokens
from third_party_management.domain.connections import SealedTokens

KEY_BYTES = 32
NONCE_BYTES = 12
ACCESS = b"access"
REFRESH = b"refresh"
# Subclave del `state` OAuth (R): separa el uso HMAC del uso AES-GCM de la misma clave.
STATE_KEY_INFO = b"sportapp-strava-oauth-state.v1"


class InvalidEncryptionKey(ValueError):
    """La clave no es base64 URL-safe de 32 bytes."""


class UndecryptableToken(ValueError):
    """El texto cifrado no corresponde a la clave o a sus datos asociados."""


def decode_key(encoded: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(encoded.encode("ascii") + b"=" * (-len(encoded) % 4))
    except (binascii.Error, UnicodeError, ValueError):
        raise InvalidEncryptionKey("clave ilegible") from None
    if len(key) != KEY_BYTES:
        raise InvalidEncryptionKey("la clave debe tener 32 bytes")
    return key


def derive_state_key(key: bytes) -> bytes:
    """HKDF-SHA256 de 32 bytes sin sal con `info = sportapp-strava-oauth-state.v1`."""
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=STATE_KEY_INFO).derive(
        key
    )


def associated_data(user_id: UUID, kind: bytes) -> bytes:
    return user_id.bytes + b":" + kind


class TokenCipher:
    def __init__(self, key: bytes) -> None:
        self._aead = AESGCM(key)

    def encrypt(self, token: str, aad: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, token.encode("utf-8"), aad)

    def decrypt(self, ciphertext: bytes, aad: bytes) -> str:
        nonce, sealed = ciphertext[:NONCE_BYTES], ciphertext[NONCE_BYTES:]
        try:
            return self._aead.decrypt(nonce, sealed, aad).decode("utf-8")
        except (InvalidTag, ValueError):
            raise UndecryptableToken() from None


class AesGcmTokenSealer:
    """Implementa `TokenSealer`: los tokens en claro solo existen en memoria."""

    def __init__(self, key: bytes) -> None:
        self._cipher = TokenCipher(key)

    def seal(self, user_id: UUID, tokens: ProviderTokens) -> SealedTokens:
        return SealedTokens(
            access=self._cipher.encrypt(tokens.access_token, associated_data(user_id, ACCESS)),
            refresh=self._cipher.encrypt(tokens.refresh_token, associated_data(user_id, REFRESH)),
            expires_at=tokens.expires_at,
        )

    def open(self, user_id: UUID, sealed: SealedTokens) -> ProviderTokens:
        return ProviderTokens(
            access_token=self._cipher.decrypt(sealed.access, associated_data(user_id, ACCESS)),
            refresh_token=self._cipher.decrypt(sealed.refresh, associated_data(user_id, REFRESH)),
            expires_at=sealed.expires_at,
        )
