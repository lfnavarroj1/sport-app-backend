"""Reglas comunes a las secciones del perfil: versión propia y documento completo.

Cada sección se registra y reemplaza completa (`PUT`). Su documento es a la vez el
cuerpo de la respuesta del comando y el payload del evento (ADR-034), con los decimales
como cadena y los instantes en UTC con sufijo `Z`.
"""

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from users_management.domain.profile.validation import format_instant

INITIAL_VERSION = 1
# `issue` de `version_conflict` cuando la sección todavía no existe.
NO_SECTION_VERSION = 0


class VersionConflict(Exception):
    """`expected_version` no coincide con la versión vigente (control optimista)."""

    code = "version_conflict"

    def __init__(self, current_version: int) -> None:
        super().__init__(f"current_version={current_version}")
        self.current_version = current_version


class SectionNotFound(Exception):
    """El comando edita una sección que todavía no existe (HU008: primero HU006)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def next_version(expected_version: int | None, current_version: int | None) -> int:
    """Versión que tendrá la sección; un conflicto nunca se sobrescribe en silencio.

    - La sección existe y `expected_version` falta o difiere → conflicto.
    - La sección no existe y `expected_version` viene informado → conflicto.
    """
    if current_version is None:
        if expected_version is not None:
            raise VersionConflict(NO_SECTION_VERSION)
        return INITIAL_VERSION
    if expected_version != current_version:
        raise VersionConflict(current_version)
    return current_version + 1


class ValidSection(Protocol):
    """Sección validada y normalizada, lista para persistir."""

    @property
    def expected_version(self) -> int | None: ...

    def normalized(self) -> dict[str, Any]:
        """Payload normalizado sin instantes del comando; base de la huella idempotente."""
        ...

    def fields(self, now: datetime) -> dict[str, Any]:
        """Campos propios de la sección en el formato del documento."""
        ...


def section_document(
    *,
    user_id: UUID,
    version: int,
    fields: dict[str, Any],
    created_at: str,
    updated_at: datetime,
) -> dict[str, Any]:
    return {
        "user_id": str(user_id),
        "version": version,
        **fields,
        "created_at": created_at,
        "updated_at": format_instant(updated_at),
    }
