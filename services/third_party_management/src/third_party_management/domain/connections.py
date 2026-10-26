"""Vinculación entre un deportista y su cuenta de Strava (HU031, ADR-042 §2-§7).

- Scopes mínimos `read,activity:read`; sin `activity:read` no hay vinculación (R: el
  callback responde `DENIED`).
- El `state` antifalsificación vence a los 10 minutos y se usa una sola vez.
- Los tokens solo existen cifrados (`SealedTokens`); el dominio nunca ve su texto.
- `ACTIVE` ⇔ hay tokens: al desvincular (`REVOKED`) o al expirar la autorización
  (`EXPIRED`, R) los tokens se borran.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

REQUESTED_SCOPES = ("read", "activity:read")
REQUIRED_SCOPE = "activity:read"
OAUTH_STATE_TTL = timedelta(minutes=10)
# ADR-042 §9: se renueva el access token cuando le faltan menos de 5 minutos.
REFRESH_MARGIN = timedelta(minutes=5)


class AccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


NOT_LINKED = "NOT_LINKED"


def parse_scopes(raw: str | None) -> tuple[str, ...]:
    """Lista separada por comas tal como la devuelve Strava, sin repetidos ni vacíos."""
    if not raw:
        return ()
    seen: list[str] = []
    for item in raw.split(","):
        scope = item.strip()
        if scope and scope not in seen:
            seen.append(scope)
    return tuple(seen)


def grants_activity_read(scopes: tuple[str, ...]) -> bool:
    return REQUIRED_SCOPE in scopes


def state_expired(created_at: datetime, now: datetime) -> bool:
    return now - created_at > OAUTH_STATE_TTL


@dataclass(frozen=True)
class SealedTokens:
    """Tokens cifrados con AES-256-GCM; nunca se imprimen."""

    access: bytes = field(repr=False)
    refresh: bytes = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class ExternalAccount:
    user_id: UUID
    external_athlete_id: str
    tokens: SealedTokens | None
    scopes: tuple[str, ...]
    consent_version: str
    status: AccountStatus
    version: int
    linked_at: datetime
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_active(self) -> bool:
        return self.status is AccountStatus.ACTIVE and self.tokens is not None

    @classmethod
    def link(
        cls,
        *,
        current: "ExternalAccount | None",
        user_id: UUID,
        external_athlete_id: str,
        tokens: SealedTokens,
        scopes: tuple[str, ...],
        consent_version: str,
        now: datetime,
    ) -> "ExternalAccount":
        """CA2: crea la vinculación o reemplaza la anterior con la versión siguiente."""
        return cls(
            user_id=user_id,
            external_athlete_id=external_athlete_id,
            tokens=tokens,
            scopes=scopes,
            consent_version=consent_version,
            status=AccountStatus.ACTIVE,
            version=1 if current is None else current.version + 1,
            linked_at=now,
            revoked_at=None,
            created_at=now if current is None else current.created_at,
            updated_at=now,
        )

    def revoke(self, now: datetime) -> "ExternalAccount":
        """CA9: borra los tokens y marca `REVOKED` (una cuenta ya revocada sigue igual)."""
        if self.status is AccountStatus.REVOKED and self.tokens is None:
            return self
        return replace(
            self,
            tokens=None,
            status=AccountStatus.REVOKED,
            version=self.version + 1,
            revoked_at=now,
            updated_at=now,
        )

    def expire(self, now: datetime) -> "ExternalAccount":
        """Strava rechazó la autorización (401 tras renovar o renovación rechazada)."""
        return replace(
            self,
            tokens=None,
            status=AccountStatus.EXPIRED,
            version=self.version + 1,
            updated_at=now,
        )

    def with_tokens(self, tokens: SealedTokens, now: datetime) -> "ExternalAccount":
        """Tokens renovados por el worker; la vinculación no cambia de versión (R)."""
        return replace(self, tokens=tokens, updated_at=now)
