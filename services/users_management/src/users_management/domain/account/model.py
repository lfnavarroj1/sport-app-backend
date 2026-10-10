"""Agregado `UserAccount` y reglas de registro (HU001)."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from users_management.domain.account.errors import (
    InvalidRegistrationData,
    PoliciesVersionMismatch,
)

FULL_NAME_MAX_LENGTH = 120
EMAIL_MAX_LENGTH = 254
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128


class ActorType(StrEnum):
    ATHLETE = "athlete"
    ORGANIZER = "organizer"
    SERVICE_PROVIDER = "service_provider"


class AccountStatus(StrEnum):
    ACTIVE = "active"


def normalize_email(email: str) -> str:
    """El correo es único sin distinguir mayúsculas ni espacios exteriores."""
    normalized = email.strip().lower()
    if not normalized or len(normalized) > EMAIL_MAX_LENGTH or "@" not in normalized:
        raise InvalidRegistrationData("email", "invalid_email")
    return normalized


def normalize_full_name(full_name: str) -> str:
    normalized = " ".join(full_name.split())
    if not normalized or len(normalized) > FULL_NAME_MAX_LENGTH:
        raise InvalidRegistrationData("full_name", "invalid_length")
    return normalized


def validate_password(password: str) -> None:
    """Política NIST 800-63B aprobada: solo longitud, sin reglas de composición."""
    if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        raise InvalidRegistrationData("password", "invalid_length")


def validate_policies_acceptance(accepted_version: str, current_version: str) -> None:
    if accepted_version != current_version:
        raise PoliciesVersionMismatch()


@dataclass(frozen=True)
class UserAccount:
    user_id: UUID
    full_name: str
    email: str
    actor_type: ActorType
    status: AccountStatus
    password_hash: str
    accepted_policies_version: str
    policies_accepted_at: datetime
    created_at: datetime
    version: int

    @classmethod
    def register(
        cls,
        *,
        user_id: UUID,
        full_name: str,
        email: str,
        actor_type: ActorType,
        password_hash: str,
        accepted_policies_version: str,
        now: datetime,
    ) -> "UserAccount":
        """Crea la cuenta en versión 1. La contraseña llega ya protegida por hash."""
        return cls(
            user_id=user_id,
            full_name=normalize_full_name(full_name),
            email=normalize_email(email),
            actor_type=actor_type,
            status=AccountStatus.ACTIVE,
            password_hash=password_hash,
            accepted_policies_version=accepted_policies_version,
            policies_accepted_at=now,
            created_at=now,
            version=1,
        )
