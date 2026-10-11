"""Base de escritura migrada y caso de uso real para pruebas de integración."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import Engine, create_engine, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.postgres import temporary_database
from users_management.application.commands.login import Login
from users_management.application.commands.refresh_session import RefreshSession
from users_management.application.commands.register_user import (
    RegisterUser,
    RegisterUserCommand,
)
from users_management.application.ports import RegistrationUnitOfWork
from users_management.domain.account.model import AccountStatus, ActorType
from users_management.domain.authentication.model import LoginThrottlePolicy
from users_management.infrastructure.clock import SystemClock
from users_management.infrastructure.persistence_write.authentication import (
    SqlAlchemyAuthenticationUnitOfWork,
)
from users_management.infrastructure.persistence_write.models import (
    IdempotencyKeyRow,
    OutboxEventRow,
    RefreshTokenRow,
    UserAccountRow,
)
from users_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyRegistrationUnitOfWork,
)
from users_management.infrastructure.security.access_tokens import Ed25519AccessTokenIssuer
from users_management.infrastructure.security.password_hashing import Argon2idPasswordHasher

WRITE_HISTORY = "users_management.infrastructure.persistence_write:migrations"
POLICIES = "2026-10"
THROTTLE = LoginThrottlePolicy(max_failures=5, window=timedelta(minutes=15))
PASSWORDS = Argon2idPasswordHasher()
TOKENS = Ed25519AccessTokenIssuer(
    Ed25519PrivateKey.generate(),
    issuer="sportapp-users-management",
    audience="sportapp",
    ttl=timedelta(minutes=15),
)


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

    def login(self, throttle: LoginThrottlePolicy = THROTTLE) -> Login:
        return Login(
            unit_of_work=self.authentication,
            password_verifier=PASSWORDS,
            token_issuer=TOKENS,
            clock=SystemClock(),
            throttle=throttle,
            refresh_token_ttl=timedelta(days=7),
        )

    def refresh_session(self) -> RefreshSession:
        return RefreshSession(
            unit_of_work=self.authentication,
            token_issuer=TOKENS,
            clock=SystemClock(),
            refresh_token_ttl=timedelta(days=7),
        )

    def authentication(self) -> SqlAlchemyAuthenticationUnitOfWork:
        return SqlAlchemyAuthenticationUnitOfWork(self.sessions)

    def disable(self, user_id: UUID) -> None:
        with self.sessions() as session:
            session.execute(
                update(UserAccountRow)
                .where(UserAccountRow.user_id == user_id)
                .values(status=AccountStatus.DISABLED.value)
            )
            session.commit()

    def count(
        self,
        model: type[UserAccountRow | OutboxEventRow | IdempotencyKeyRow | RefreshTokenRow],
    ) -> int:
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
