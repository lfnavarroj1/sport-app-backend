"""Puertos que la aplicación necesita; la infraestructura los implementa."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID

from users_management.domain.account.model import UserAccount
from users_management.domain.authentication.model import FailureWindow, RefreshToken


class PasswordHasher(Protocol):
    def hash(self, password: str) -> str: ...


class PasswordVerifier(Protocol):
    def verify(self, password_hash: str | None, password: str) -> bool:
        """Sin hash (correo inexistente) verifica contra uno ficticio y devuelve `False`,
        para que el tiempo de respuesta no revele si la cuenta existe."""
        ...


@dataclass(frozen=True)
class IssuedAccessToken:
    token: str
    expires_at: datetime

    def __repr__(self) -> str:
        return f"IssuedAccessToken(expires_at={self.expires_at.isoformat()})"


class AccessTokenIssuer(Protocol):
    def issue(self, user_id: UUID, actor_type: str, now: datetime) -> IssuedAccessToken: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class OutboxMessage:
    """Evento pendiente de publicar; se confirma en la misma transacción del agregado."""

    event_id: UUID
    event_type: str
    schema_version: int
    aggregate_type: str
    aggregate_id: UUID
    aggregate_version: int
    occurred_at: datetime
    correlation_id: UUID | None
    causation_id: str | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class IdempotencyRecord:
    key: UUID
    operation: str
    request_fingerprint: str
    resource_id: UUID
    response: dict[str, Any]
    created_at: datetime
    expires_at: datetime


class RegistrationUnitOfWork(Protocol):
    """Transacción local del registro: cuenta, outbox e idempotencia juntas."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def find_idempotency(self, key: UUID) -> IdempotencyRecord | None: ...

    def delete_idempotency(self, key: UUID) -> None: ...

    def email_exists(self, email: str) -> bool: ...

    def add_account(self, account: UserAccount) -> None: ...

    def add_outbox(self, message: OutboxMessage) -> None: ...

    def add_idempotency(self, record: IdempotencyRecord) -> None: ...

    def commit(self) -> None:
        """Confirma todo o nada.

        Traduce violaciones de unicidad a `EmailAlreadyRegistered` o
        `IdempotencyKeyConflict` para que la aplicación no conozca SQL.
        """
        ...


class AuthenticationUnitOfWork(Protocol):
    """Transacción local corta del inicio de sesión (HU003); nunca abarca el hash ni la firma."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def find_account_by_email(self, email: str) -> UserAccount | None: ...

    def get_account(self, user_id: UUID) -> UserAccount | None: ...

    def find_failure_window(self, throttle_key: str) -> FailureWindow | None: ...

    def record_failure(self, throttle_key: str, now: datetime, window: timedelta) -> FailureWindow:
        """Suma un fallo de forma atómica aunque haya intentos concurrentes."""
        ...

    def clear_failures(self, throttle_key: str) -> None: ...

    def add_refresh_token(self, token: RefreshToken) -> None: ...

    def find_refresh_token_for_update(self, token_hash: str) -> RefreshToken | None:
        """Bloquea la fila hasta el fin de la transacción: dos renovaciones con el mismo
        token se serializan y la segunda lo encuentra ya usado."""
        ...

    def mark_refresh_token_used(
        self, token_id: UUID, used_at: datetime, replaced_by: UUID
    ) -> None: ...

    def revoke_refresh_family(self, family_id: UUID, revoked_at: datetime) -> None: ...

    def commit(self) -> None: ...
