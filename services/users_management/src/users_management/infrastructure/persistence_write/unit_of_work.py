"""Unidad de trabajo SQLAlchemy para el registro de usuarios."""

from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy import delete, exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from users_management.application.errors import IdempotencyKeyConflict
from users_management.application.ports import IdempotencyRecord, OutboxMessage
from users_management.domain.account.errors import EmailAlreadyRegistered
from users_management.domain.account.model import UserAccount
from users_management.infrastructure.persistence_write.models import (
    EMAIL_UNIQUE_CONSTRAINT,
    IDEMPOTENCY_PRIMARY_KEY,
    IdempotencyKeyRow,
    OutboxEventRow,
    UserAccountRow,
)


def _violated_constraint(error: IntegrityError) -> str | None:
    diagnostics = getattr(error.orig, "diag", None)
    return getattr(diagnostics, "constraint_name", None)


class SqlAlchemyRegistrationUnitOfWork:
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

    def find_idempotency(self, key: UUID) -> IdempotencyRecord | None:
        row = self.session.get(IdempotencyKeyRow, key)
        if row is None:
            return None
        return IdempotencyRecord(
            key=row.key,
            operation=row.operation,
            request_fingerprint=row.request_fingerprint,
            resource_id=row.resource_id,
            response=row.response,
            created_at=row.created_at,
            expires_at=row.expires_at,
        )

    def delete_idempotency(self, key: UUID) -> None:
        self.session.execute(delete(IdempotencyKeyRow).where(IdempotencyKeyRow.key == key))

    def email_exists(self, email: str) -> bool:
        return bool(self.session.scalar(select(exists().where(UserAccountRow.email == email))))

    def add_account(self, account: UserAccount) -> None:
        self.session.add(
            UserAccountRow(
                user_id=account.user_id,
                full_name=account.full_name,
                email=account.email,
                actor_type=account.actor_type.value,
                status=account.status.value,
                password_hash=account.password_hash,
                accepted_policies_version=account.accepted_policies_version,
                policies_accepted_at=account.policies_accepted_at,
                created_at=account.created_at,
                updated_at=account.created_at,
                version=account.version,
            )
        )

    def add_outbox(self, message: OutboxMessage) -> None:
        self.session.add(
            OutboxEventRow(
                event_id=message.event_id,
                aggregate_type=message.aggregate_type,
                aggregate_id=message.aggregate_id,
                aggregate_version=message.aggregate_version,
                event_type=message.event_type,
                schema_version=message.schema_version,
                occurred_at=message.occurred_at,
                correlation_id=message.correlation_id,
                causation_id=message.causation_id,
                payload=message.payload,
                attempts=0,
                next_attempt_at=message.occurred_at,
            )
        )

    def add_idempotency(self, record: IdempotencyRecord) -> None:
        self.session.add(
            IdempotencyKeyRow(
                key=record.key,
                operation=record.operation,
                request_fingerprint=record.request_fingerprint,
                resource_id=record.resource_id,
                response=record.response,
                created_at=record.created_at,
                expires_at=record.expires_at,
            )
        )

    def commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            constraint = _violated_constraint(error)
            if constraint == EMAIL_UNIQUE_CONSTRAINT:
                raise EmailAlreadyRegistered() from None
            if constraint == IDEMPOTENCY_PRIMARY_KEY:
                raise IdempotencyKeyConflict() from None
            raise
