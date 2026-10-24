"""HU022 (ADR-039): nombre visible de un lote de cuentas activas (contrato interno).

`POST /v1/users/directory-lookup` lo consume solo `bff-web` para componer `display_name` en la
lista de inscritos del organizador, con los `athlete_id` que devuelve Eventos; nunca con
identificadores del navegador. Devuelve **solo** `full_name` (identidad mínima de ADR-039): ni
correo, ni perfil, ni datos de salud. Por eso no reutiliza `lookup-batch` (HU011), que también
entrega el correo.

Lee la base de lectura (`user_directory`, columna `full_name` de ADR-035). Entre 1 y 100
identificadores únicos (la misma validación de `lookup-batch`); una cuenta inexistente,
deshabilitada o aún sin nombre proyectado se omite. Cada consulta se audita
(`audit.directory_lookup`) con el número de identificadores pedidos y encontrados, nunca con
nombres ni identificadores.
"""

import logging
from dataclasses import dataclass
from uuid import UUID

from users_management.application.queries.lookup_users_batch import LookupUsersByIds

_audit = logging.getLogger("sportapp.audit")


@dataclass(frozen=True)
class DirectoryName:
    user_id: UUID
    full_name: str

    def __repr__(self) -> str:
        # El nombre es un dato personal: nunca en trazas.
        return f"DirectoryName(user_id={self.user_id})"


class LookupDirectoryNames:
    def __init__(self, lookup: LookupUsersByIds) -> None:
        self._lookup = lookup

    def __call__(self, user_ids: list[UUID]) -> list[DirectoryName]:
        found = self._lookup(user_ids)  # Valida el lote (`InvalidLookupBatch`).
        _audit.info(
            "audit.directory_lookup",
            extra={"fields": {"requested": len(user_ids), "found": len(found)}},
        )
        return [DirectoryName(user_id=item.user_id, full_name=item.full_name) for item in found]
