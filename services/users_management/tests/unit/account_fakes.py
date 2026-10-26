"""Dobles en memoria de HU002: cuenta editable, outbox, idempotencia y directorio."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

from fakes import FixedClock

from users_management.application.commands.update_account import UpdateAccount
from users_management.application.errors import IdempotencyKeyConflict
from users_management.application.ports import AccountView, IdempotencyRecord, OutboxMessage
from users_management.domain.account.errors import EmailAlreadyRegistered
from users_management.domain.account.model import AccountDetails, AccountStatus, ActorType

CREATED_AT = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def account_details(
    *,
    user_id: UUID | None = None,
    full_name: str = "Ana Pérez",
    email: str = "ana@example.com",
    actor_type: ActorType = ActorType.ATHLETE,
    version: int = 1,
) -> AccountDetails:
    return AccountDetails(
        user_id=user_id or uuid4(),
        full_name=full_name,
        email=email,
        actor_type=actor_type,
        status=AccountStatus.ACTIVE,
        version=version,
        updated_at=CREATED_AT,
    )


@dataclass
class InMemoryAccountStore:
    accounts: dict[UUID, AccountDetails] = field(default_factory=dict)
    idempotency: dict[UUID, IdempotencyRecord] = field(default_factory=dict)
    outbox: list[OutboxMessage] = field(default_factory=list)
    commits: int = 0

    def add(self, account: AccountDetails) -> AccountDetails:
        self.accounts[account.user_id] = account
        return account


class InMemoryAccountUnitOfWork:
    """Aplica los cambios solo en `commit`, como la transacción real.

    `pending` guarda, como mucho, una acción de otra transacción que confirma justo antes
    del primer `commit` (simula carreras); se consume una sola vez.
    """

    def __init__(
        self, store: InMemoryAccountStore, pending: list[Callable[[], None]] | None = None
    ) -> None:
        self._store = store
        self._pending = pending if pending is not None else []
        self._account: AccountDetails | None = None
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
        return None

    def find_idempotency(self, key: UUID) -> IdempotencyRecord | None:
        return self._store.idempotency.get(key)

    def delete_idempotency(self, key: UUID) -> None:
        self._deleted.append(key)

    def lock_account(self, user_id: UUID) -> AccountDetails | None:
        return self._store.accounts.get(user_id)

    def email_taken(self, email: str, user_id: UUID) -> bool:
        return any(
            account.email == email and account.user_id != user_id
            for account in self._store.accounts.values()
        )

    def save_account(self, account: AccountDetails) -> None:
        self._account = account

    def add_outbox(self, message: OutboxMessage) -> None:
        self._outbox.append(message)

    def add_idempotency(self, record: IdempotencyRecord) -> None:
        self._idempotency.append(record)

    def commit(self) -> None:
        if self._pending:
            self._pending.pop()()
        for record in self._idempotency:
            if record.key in self._store.idempotency and record.key not in self._deleted:
                raise IdempotencyKeyConflict()
        if self._account is not None and self.email_taken(
            self._account.email, self._account.user_id
        ):
            raise EmailAlreadyRegistered()
        for key in self._deleted:
            self._store.idempotency.pop(key, None)
        if self._account is not None:
            self._store.accounts[self._account.user_id] = self._account
        self._store.outbox.extend(self._outbox)
        for record in self._idempotency:
            self._store.idempotency[record.key] = record
        self._store.commits += 1


def update_account_with(
    store: InMemoryAccountStore,
    clock: FixedClock | None = None,
    before_commit: Callable[[], None] | None = None,
) -> UpdateAccount:
    pending = [before_commit] if before_commit is not None else []
    return UpdateAccount(
        unit_of_work=lambda: InMemoryAccountUnitOfWork(store, pending), clock=clock or FixedClock()
    )


class InMemoryAccountReader:
    def __init__(self) -> None:
        self.views: dict[UUID, AccountView] = {}
        self.unavailable: Exception | None = None

    def get(self, user_id: UUID) -> AccountView | None:
        if self.unavailable is not None:
            raise self.unavailable
        return self.views.get(user_id)
