"""Base de escritura migrada y caso de uso real para pruebas de integración."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.postgres import temporary_database
from users_management.application.commands.register_user import (
    RegisterUser,
    RegisterUserCommand,
)
from users_management.application.ports import RegistrationUnitOfWork
from users_management.domain.account.model import ActorType
from users_management.infrastructure.clock import SystemClock
from users_management.infrastructure.persistence_write.models import (
    IdempotencyKeyRow,
    OutboxEventRow,
    UserAccountRow,
)
from users_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyRegistrationUnitOfWork,
)
from users_management.infrastructure.security.password_hashing import Argon2idPasswordHasher

WRITE_HISTORY = "users_management.infrastructure.persistence_write:migrations"
POLICIES = "2026-10"


@dataclass
class WriteDatabase:
    url: str
    engine: Engine
    sessions: sessionmaker[Session]

    def register_user(
        self,
        unit_of_work: Callable[[], RegistrationUnitOfWork] | None = None,
    ) -> RegisterUser:
        return RegisterUser(
            unit_of_work=unit_of_work or (lambda: SqlAlchemyRegistrationUnitOfWork(self.sessions)),
            password_hasher=Argon2idPasswordHasher(),
            clock=SystemClock(),
            current_policies_version=POLICIES,
        )

    def count(self, model: type[UserAccountRow | OutboxEventRow | IdempotencyKeyRow]) -> int:
        with self.sessions() as session:
            return session.scalar(select(func.count()).select_from(model)) or 0


@contextmanager
def write_database(prefix: str) -> Iterator[WriteDatabase]:
    with temporary_database(prefix) as url:
        upgrade(WRITE_HISTORY, url)
        engine = create_engine(url, pool_size=10, max_overflow=10)
        try:
            yield WriteDatabase(url, engine, sessionmaker(engine, expire_on_commit=False))
        finally:
            engine.dispose()


def registration(**overrides: object) -> RegisterUserCommand:
    values: dict[str, object] = {
        "full_name": "Ana Pérez",
        "email": f"ana-{uuid4().hex[:8]}@example.com",
        "password": "contraseña-muy-secreta",
        "actor_type": ActorType.ATHLETE,
        "accepted_policies_version": POLICIES,
        "idempotency_key": uuid4(),
        "correlation_id": uuid4(),
    }
    values.update(overrides)
    return RegisterUserCommand(**values)  # type: ignore[arg-type]
