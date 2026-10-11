"""Claves de firma desechables para pruebas: se generan en memoria y nunca se versionan."""

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat


def ed25519_private_key_pem() -> str:
    generated = Ed25519PrivateKey.generate()
    return generated.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
