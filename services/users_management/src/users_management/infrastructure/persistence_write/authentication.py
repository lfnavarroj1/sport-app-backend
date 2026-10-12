"""Unidad de trabajo SQLAlchemy para el inicio de sesión (HU003)."""

from datetime import datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy import case, delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from users_management.domain.account.model import AccountStatus, ActorType, UserAccount
from users_management.domain.authentication.model import FailureWindow, RefreshToken
from users_management.infrastructure.persistence_write.models import (
    LoginFailureWindowRow,
    RefreshTokenRow,
    UserAccountRow,
)


def _to_account(row: UserAccountRow) -> UserAccount:
    return UserAccount(
        user_id=row.user_id,
        full_name=row.full_name,
        email=row.email,
        actor_type=ActorType(row.actor_type),
        status=AccountStatus(row.status),
        password_hash=row.password_hash,
        accepted_policies_version=row.accepted_policies_version,
        policies_accepted_at=row.policies_accepted_at,
        created_at=row.created_at,
        version=row.version,
    )


def _to_refresh_token(row: RefreshTokenRow) -> RefreshToken:
    return RefreshToken(
        token_id=row.token_id,
        family_id=row.family_id,
        user_id=row.user_id,
        token_hash=row.token_hash,
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        used_at=row.used_at,
        revoked_at=row.revoked_at,
    )


class SqlAlchemyAuthenticationUnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None

    @property
    def session(self) -> Session:
        if self._session is None:
            raise RuntimeError("La unidad de trabajo no está abierta.")
        return self._session

    def __enter__(self) -> Self:
        self._session = self._session_factory()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # Todo lo no confirmado explícitamente se descarta.
        self.session.rollback()
        self.session.close()
        self._session = None

    def find_account_by_email(self, email: str) -> UserAccount | None:
        row = self.session.scalar(select(UserAccountRow).where(UserAccountRow.email == email))
        return _to_account(row) if row else None

    def get_account(self, user_id: UUID) -> UserAccount | None:
        row = self.session.get(UserAccountRow, user_id)
        return _to_account(row) if row else None

    def find_failure_window(self, throttle_key: str) -> FailureWindow | None:
        row = self.session.get(LoginFailureWindowRow, throttle_key)
        if row is None:
            return None
        return FailureWindow(failures=row.failures, started_at=row.window_started_at)

    def record_failure(self, throttle_key: str, now: datetime, window: timedelta) -> FailureWindow:
        # Un solo `INSERT ... ON CONFLICT DO UPDATE`: intentos concurrentes no pierden
        # incrementos. Misma regla que `LoginThrottlePolicy.after_failure`.
        table = LoginFailureWindowRow.__table__
        expired = table.c.window_started_at <= now - window
        statement = (
            insert(LoginFailureWindowRow)
            .values(throttle_key=throttle_key, failures=1, window_started_at=now, updated_at=now)
            .on_conflict_do_update(
                index_elements=[table.c.throttle_key],
                set_={
                    "failures": case((expired, 1), else_=table.c.failures + 1),
                    "window_started_at": case((expired, now), else_=table.c.window_started_at),
                    "updated_at": now,
                },
            )
            .returning(table.c.failures, table.c.window_started_at)
        )
        failures, started_at = self.session.execute(statement).one()
        return FailureWindow(failures=failures, started_at=started_at)

    def clear_failures(self, throttle_key: str) -> None:
        self.session.execute(
            delete(LoginFailureWindowRow).where(LoginFailureWindowRow.throttle_key == throttle_key)
        )

    def add_refresh_token(self, token: RefreshToken) -> None:
        self.session.add(
            RefreshTokenRow(
                token_id=token.token_id,
                family_id=token.family_id,
                user_id=token.user_id,
                token_hash=token.token_hash,
                issued_at=token.issued_at,
                expires_at=token.expires_at,
                used_at=token.used_at,
                revoked_at=token.revoked_at,
            )
        )

    def find_refresh_token_for_update(self, token_hash: str) -> RefreshToken | None:
        row = self.session.scalar(
            select(RefreshTokenRow)
            .where(RefreshTokenRow.token_hash == token_hash)
            .with_for_update()
        )
        return _to_refresh_token(row) if row else None

    def mark_refresh_token_used(self, token_id: UUID, used_at: datetime, replaced_by: UUID) -> None:
        self.session.execute(
            update(RefreshTokenRow)
            .where(RefreshTokenRow.token_id == token_id)
            .values(used_at=used_at, replaced_by=replaced_by)
        )

    def lock_refresh_family(self, family_id: UUID) -> None:
        self.session.execute(
            select(RefreshTokenRow.token_id)
            .where(RefreshTokenRow.family_id == family_id)
            .with_for_update()
        )

    def revoke_refresh_family(self, family_id: UUID, revoked_at: datetime) -> None:
        self.session.execute(
            update(RefreshTokenRow)
            .where(RefreshTokenRow.family_id == family_id, RefreshTokenRow.revoked_at.is_(None))
            .values(revoked_at=revoked_at)
        )

    def commit(self) -> None:
        self.session.commit()
