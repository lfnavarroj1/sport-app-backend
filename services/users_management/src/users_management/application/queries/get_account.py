"""HU002: `GET /v1/me/account` sobre la proyección `user_directory` (ADR-035 §6).

Consulta eventual: lee solo la base de lectura, nunca la de escritura. Tras un comando, el
cliente usa la respuesta del `PUT` y la consulta refleja el cambio una vez proyectado. Sin
fila (o sin nombre proyectado), `None`; si la base de lectura no responde,
`ReadModelUnavailable` (→ `503`).
"""

from uuid import UUID

from users_management.application.ports import AccountReader, AccountView


class GetAccount:
    def __init__(self, reader: AccountReader) -> None:
        self._reader = reader

    def __call__(self, user_id: UUID) -> AccountView | None:
        return self._reader.get(user_id)
