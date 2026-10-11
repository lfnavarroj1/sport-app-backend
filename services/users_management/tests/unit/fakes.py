"""Dobles en memoria para probar casos de uso sin infraestructura."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID

from users_management.application.commands.register_user import RegisterUser
from users_management.application.errors import IdempotencyKeyConflict
from users_management.application.ports import IdempotencyRecord, OutboxMessage
from users_management.domain.account.errors import EmailAlreadyRegistered
from users_management.domain.account.model import UserAccount

CURRENT_POLICIES = "2026-10"


@dataclass
class InMemoryStore:
    accounts: dict[UUID, UserAccount] = field(default_factory=dict)
    outbox: list[OutboxMessage] = field(default_factory=list)
    idempotency: dict[UUID, IdempotencyRecord] = field(default_factory=dict)


class InMemoryUnitOfWork:
    """Aplica los cambios solo al confirmar y valida las mismas unicidades que PostgreSQL."""

    def __init__(self, store: InMemoryStore, fail_on_commit: Exception | None = None) -> None:
        self.store = store
        self.fail_on_commit = fail_on_commit
        self._accounts: list[UserAccount] = []
        self._outbox: list[OutboxMessage] = []
        self._idempotency: list[IdempotencyRecord] = []
        self._deleted: list[UUID] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._accounts, self._outbox, self._idempotency, self._deleted = [], [], [], []

    def find_idempotency(self, key: UUID) -> IdempotencyRecord | None:
        return self.store.idempotency.get(key)

    def delete_idempotency(self, key: UUID) -> None:
        self._deleted.append(key)

    def email_exists(self, email: str) -> bool:
        return any(account.email == email for account in self.store.accounts.values())

    def add_account(self, account: UserAccount) -> None:
        self._accounts.append(account)

    def add_outbox(self, message: OutboxMessage) -> None:
        self._outbox.append(message)

    def add_idempotency(self, record: IdempotencyRecord) -> None:
        self._idempotency.append(record)

    def commit(self) -> None:
        if self.fail_on_commit is not None:
            raise self.fail_on_commit
        for account in self._accounts:
            if self.email_exists(account.email):
                raise EmailAlreadyRegistered()
        for key in self._deleted:
            self.store.idempotency.pop(key, None)
        for record in self._idempotency:
            if record.key in self.store.idempotency:
                raise IdempotencyKeyConflict()
        for account in self._accounts:
            self.store.accounts[account.user_id] = account
        self.store.outbox.extend(self._outbox)
        for record in self._idempotency:
            self.store.idempotency[record.key] = record


class FakeHasher:
    def __init__(self) -> None:
        self.calls = 0

    def hash(self, password: str) -> str:
        self.calls += 1
        return f"$fake$hash-{self.calls}"


class FixedClock:
    def __init__(self, now: datetime | None = None) -> None:
        self.current = now or datetime(2026, 10, 7, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def register_user_with(
    store: InMemoryStore,
    hasher: FakeHasher | None = None,
    clock: FixedClock | None = None,
) -> RegisterUser:
    return RegisterUser(
        unit_of_work=lambda: InMemoryUnitOfWork(store),
        password_hasher=hasher or FakeHasher(),
        clock=clock or FixedClock(),
        current_policies_version=CURRENT_POLICIES,
    )
