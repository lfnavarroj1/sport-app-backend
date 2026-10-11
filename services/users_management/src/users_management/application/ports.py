"""Puertos que la aplicación necesita; la infraestructura los implementa."""

from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID

from users_management.domain.account.model import UserAccount


class PasswordHasher(Protocol):
    def hash(self, password: str) -> str: ...


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
