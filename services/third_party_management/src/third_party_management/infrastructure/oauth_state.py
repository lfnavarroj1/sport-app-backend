"""`state` OAuth antifalsificación de HU031 (ADR-042 §5).

`state = base64url(r) + "." + base64url(HMAC-SHA256(k_state, r || user_id.bytes))`, sin
relleno, con `r` de 32 bytes aleatorios y `k_state` derivada con HKDF de `STRAVA_TOKEN_KEY`
(R). El servicio guarda el `state` (un solo uso, 10 minutos) y al recibirlo comprueba que el
MAC corresponda al deportista guardado: una fila alterada no vincula otra cuenta.
"""

import base64
import binascii
import hashlib
import hmac
import os
import re
from uuid import UUID

RANDOM_BYTES = 32
MAC_BYTES = 32
# 43 caracteres base64url por cada parte de 32 bytes sin relleno.
_FORM = re.compile(r"^[A-Za-z0-9_-]{43}\.[A-Za-z0-9_-]{43}$")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class HmacStateCodec:
    def __init__(self, state_key: bytes) -> None:
        self._key = state_key

    def _mac(self, random_part: bytes, user_id: UUID) -> bytes:
        return hmac.new(self._key, random_part + user_id.bytes, hashlib.sha256).digest()

    def issue(self, user_id: UUID) -> str:
        random_part = os.urandom(RANDOM_BYTES)
        return f"{_b64(random_part)}.{_b64(self._mac(random_part, user_id))}"

    def well_formed(self, state: str) -> bool:
        return bool(_FORM.match(state))

    def verify(self, state: str, user_id: UUID) -> bool:
        if not self.well_formed(state):
            return False
        random_text, mac_text = state.split(".")
        try:
            random_part, mac = _unb64(random_text), _unb64(mac_text)
        except (binascii.Error, ValueError):
            return False
        if len(random_part) != RANDOM_BYTES or len(mac) != MAC_BYTES:
            return False
        return hmac.compare_digest(mac, self._mac(random_part, user_id))
