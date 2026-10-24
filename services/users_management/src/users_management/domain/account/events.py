"""Eventos de dominio de la cuenta.

El payload de `usuario.registrado.v1` es el mínimo aprobado: identidad y perfil básico que
necesita el proyector de lectura. `usuario.actualizado.v1` (HU002, ADR-035) lleva el estado
completo de la cuenta editable. Ninguno incluye la contraseña, su hash ni las políticas.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from users_management.domain.account.model import AccountDetails, UserAccount
from users_management.domain.profile.validation import format_instant

AGGREGATE_TYPE = "user_account"


@dataclass(frozen=True)
class UserRegistered:
    EVENT_TYPE = "usuario.registrado.v1"
    SCHEMA_VERSION = 1

    user_id: UUID
    aggregate_version: int
    full_name: str
    email: str
    actor_type: str
    status: str
    registered_at: datetime

    @classmethod
    def from_account(cls, account: UserAccount) -> "UserRegistered":
        return cls(
            user_id=account.user_id,
            aggregate_version=account.version,
            full_name=account.full_name,
            email=account.email,
            actor_type=account.actor_type.value,
            status=account.status.value,
            registered_at=account.created_at,
        )

    def payload(self) -> dict[str, Any]:
        return {
            "user_id": str(self.user_id),
            "full_name": self.full_name,
            "email": self.email,
            "actor_type": self.actor_type,
            "status": self.status,
            "registered_at": self.registered_at.isoformat(),
        }


def account_document(account: AccountDetails) -> dict[str, Any]:
    """Cuenta confirmada: cuerpo de `PUT /v1/me/account` y payload de
    `usuario.actualizado.v1`. Instantes en UTC con sufijo `Z`."""
    return {
        "user_id": str(account.user_id),
        "full_name": account.full_name,
        "email": account.email,
        "actor_type": account.actor_type.value,
        "status": account.status.value,
        "version": account.version,
        "updated_at": format_instant(account.updated_at),
    }


class UserUpdated:
    """`usuario.actualizado.v1`: `aggregate_version` = versión de la cuenta y
    `ordering_key = aggregate_id = user_id` (ADR-035)."""

    EVENT_TYPE = "usuario.actualizado.v1"
    SCHEMA_VERSION = 1
