"""Reglas de inicio de sesión (HU003): control de abuso y ciclo de vida del refresh token."""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from users_management.domain.authentication.errors import (
    InvalidRefreshToken,
    RefreshTokenReused,
)


@dataclass(frozen=True)
class FailureWindow:
    """Intentos fallidos de un mismo correo desde una misma IP, contados desde `started_at`."""

    failures: int
    started_at: datetime


@dataclass(frozen=True)
class LoginThrottlePolicy:
    """Ventana fija por correo + IP (aprobada el 2026-10-10).

    Alcanzado el máximo, se rechazan los intentos de esa combinación hasta que
    vence la ventana. No bloquea la cuenta: un atacante no puede impedir que el
    titular entre desde otra IP.
    """

    max_failures: int
    window: timedelta

    def retry_after(self, window: FailureWindow | None, now: datetime) -> int | None:
        """Segundos que faltan para volver a intentar, o `None` si se permite el intento."""
        if window is None or window.failures < self.max_failures:
            return None
        remaining = window.started_at + self.window - now
        if remaining <= timedelta(0):
            return None
        return max(1, math.ceil(remaining.total_seconds()))

    def after_failure(self, window: FailureWindow | None, now: datetime) -> FailureWindow:
        """Suma un fallo; si la ventana anterior venció, empieza una nueva."""
        if window is None or window.started_at + self.window <= now:
            return FailureWindow(failures=1, started_at=now)
        return FailureWindow(failures=window.failures + 1, started_at=window.started_at)


@dataclass(frozen=True)
class RefreshToken:
    """Refresh token persistido. Solo se guarda el hash del valor entregado al cliente.

    Los tokens de una misma sesión comparten `family_id`: cada renovación marca el
    actual como usado y emite el siguiente de la familia.
    """

    token_id: UUID
    family_id: UUID
    user_id: UUID
    token_hash: str
    issued_at: datetime
    expires_at: datetime
    used_at: datetime | None = None
    revoked_at: datetime | None = None

    @classmethod
    def issue(
        cls,
        *,
        token_id: UUID,
        family_id: UUID,
        user_id: UUID,
        token_hash: str,
        now: datetime,
        ttl: timedelta,
    ) -> "RefreshToken":
        return cls(
            token_id=token_id,
            family_id=family_id,
            user_id=user_id,
            token_hash=token_hash,
            issued_at=now,
            expires_at=now + ttl,
        )

    def ensure_usable(self, now: datetime) -> None:
        if self.revoked_at is not None or self.expires_at <= now:
            raise InvalidRefreshToken()
        if self.used_at is not None:
            raise RefreshTokenReused()
