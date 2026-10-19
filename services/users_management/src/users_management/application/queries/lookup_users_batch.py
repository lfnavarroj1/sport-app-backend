"""HU011 (CA5): nombre y correo de un lote de cuentas activas (contrato interno).

Lo consume solo `bff-web` para la lista de deportistas del profesional, con los
identificadores que devuelve Entrenamientos; nunca con identificadores del navegador.
Lee la base de lectura (`user_directory`). Entre 1 y 100 identificadores únicos; una
cuenta inexistente, deshabilitada o aún sin nombre proyectado se omite del resultado.
"""

from uuid import UUID

from users_management.application.ports import UserDisplay, UserDisplayDirectory

MAX_BATCH = 100


class InvalidLookupBatch(Exception):
    code = "validation_error"

    def __init__(self, issue: str) -> None:
        super().__init__(issue)
        self.issue = issue


class LookupUsersByIds:
    def __init__(self, directory: UserDisplayDirectory) -> None:
        self._directory = directory

    def __call__(self, user_ids: list[UUID]) -> list[UserDisplay]:
        if not 1 <= len(user_ids) <= MAX_BATCH:
            raise InvalidLookupBatch("invalid_count")
        if len(set(user_ids)) != len(user_ids):
            raise InvalidLookupBatch("duplicate_value")
        return self._directory.find_active_by_ids(tuple(user_ids))
