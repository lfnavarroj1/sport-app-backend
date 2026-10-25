"""Cifrado en reposo de los tokens FCM (HU039, ADR-045: «tokens FCM cifrados en reposo»).

AES-256-GCM con la clave `PUSH_TOKEN_ENCRYPTION_KEY` (32 bytes en base64 URL-safe) y un nonce
aleatorio de 12 bytes antepuesto al texto cifrado. `token_hash` (SHA-256 del token) permite
encontrar y reasignar un token sin descifrarlo. La clave nunca se registra ni se versiona.
"""

import base64
import binascii
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12


class InvalidEncryptionKey(ValueError):
    """La clave no es base64 URL-safe de 32 bytes."""


def decode_key(encoded: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(encoded.encode("ascii") + b"=" * (-len(encoded) % 4))
    except (binascii.Error, UnicodeError):
        raise InvalidEncryptionKey("clave ilegible") from None
    if len(key) != KEY_BYTES:
        raise InvalidEncryptionKey("la clave debe tener 32 bytes")
    return key


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenCipher:
    def __init__(self, key: bytes) -> None:
        self._aead = AESGCM(key)

    def encrypt(self, token: str) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, token.encode("utf-8"), None)

    def decrypt(self, ciphertext: bytes) -> str:
        nonce, sealed = ciphertext[:NONCE_BYTES], ciphertext[NONCE_BYTES:]
        return self._aead.decrypt(nonce, sealed, None).decode("utf-8")
