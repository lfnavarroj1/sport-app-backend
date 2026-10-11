"""Piezas comunes de HU003: resultado de sesión y refresh tokens opacos."""

import hashlib
import math
import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from users_management.application.ports import IssuedAccessToken
from users_management.domain.account.model import ActorType, UserAccount

REFRESH_TOKEN_BYTES = 32


def new_refresh_secret() -> str:
    """Valor opaco de 256 bits que se entrega una sola vez al cliente."""
    return secrets.token_urlsafe(REFRESH_TOKEN_BYTES)


def hash_refresh_token(value: str) -> str:
    """SHA-256 basta: el valor es aleatorio de alta entropía, no una contraseña."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SessionResult:
    user_id: UUID
    full_name: str
    email: str
    actor_type: ActorType
    access_token: str
    access_token_expires_in: int
    refresh_token: str
    refresh_token_expires_in: int

    @classmethod
    def build(
        cls,
        *,
        account: UserAccount,
        access: IssuedAccessToken,
        refresh_secret: str,
        refresh_expires_at: datetime,
        now: datetime,
    ) -> "SessionResult":
        return cls(
            user_id=account.user_id,
            full_name=account.full_name,
            email=account.email,
            actor_type=account.actor_type,
            access_token=access.token,
            access_token_expires_in=_seconds_until(access.expires_at, now),
            refresh_token=refresh_secret,
            refresh_token_expires_in=_seconds_until(refresh_expires_at, now),
        )

    def __repr__(self) -> str:
        # Los tokens son credenciales: nunca en trazas ni logs.
        return f"SessionResult(user_id={self.user_id})"


def _seconds_until(moment: datetime, now: datetime) -> int:
    # Hacia arriba: `exp` del JWT está en segundos enteros y `now` trae fracción.
    return max(0, math.ceil((moment - now).total_seconds()))
