"""Eventos de dominio de la cuenta.

El payload es el mínimo aprobado para `usuario.registrado.v1`: identidad y
perfil básico que necesita el proyector de lectura. Nunca incluye la
contraseña ni su hash.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from users_management.domain.account.model import UserAccount

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
