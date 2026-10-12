"""HU010: resolver un correo exacto a `user_id` y `actor_type` (contrato interno).

Lo consume `bff-web` para que un deportista autorice a un profesional y para que
el profesional asigne un plan. Solo coincidencia exacta del correo normalizado,
nunca búsqueda parcial, para no facilitar la enumeración de cuentas. Lee la base
de lectura (directorio que mantiene `usuarios-read-projector`); una cuenta
deshabilitada no se resuelve.
"""

from users_management.application.ports import UserDirectory, UserReference
from users_management.domain.account.model import normalize_email


class LookupUserByEmail:
    def __init__(self, directory: UserDirectory) -> None:
        self._directory = directory

    def __call__(self, email: str) -> UserReference | None:
        return self._directory.find_active_by_email(normalize_email(email))
