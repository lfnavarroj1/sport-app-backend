"""Unidad de trabajo SQLAlchemy de `PUT /v1/me/account` (HU002, ADR-035).

`user_account`, `outbox_event` e `idempotency_key` se confirman en una transacción. La fila
de la cuenta se bloquea (`FOR UPDATE`) para que dos ediciones simultáneas se serialicen y
solo una cree la versión siguiente; la unicidad del correo la garantiza
`uq_user_account_email` aunque dos cuentas pidan el mismo correo a la vez.
"""

from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy import delete, exists, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from users_management.application.errors import IdempotencyKeyConflict
from users_management.application.ports import IdempotencyRecord, OutboxMessage
from users_management.domain.account.errors import EmailAlreadyRegistered
from users_management.domain.account.model import AccountDetails, AccountStatus, ActorType
from users_management.infrastructure.persistence_write.models import (
    EMAIL_UNIQUE_CONSTRAINT,
    IDEMPOTENCY_PRIMARY_KEY,
    IdempotencyKeyRow,
    OutboxEventRow,
    UserAccountRow,
)


class AccountWriteFailed(RuntimeError):
    """Fallo de PostgreSQL al escribir la cuenta.

    El mensaje de SQLAlchemy incluye la sentencia y sus parámetros (nombre y correo); se
    descartan para que nunca lleguen a los logs. Solo se conservan la clase del error y la
    restricción incumplida.
    """

    def __init__(self, error: DBAPIError) -> None:
        super().__init__(f"{type(error.orig).__name__} constraint={_violated_constraint(error)}")


def _violated_constraint(error: DBAPIError) -> str | None:
    diagnostics = getattr(error.orig, "diag", None)
    return getattr(diagnostics, "constraint_name", None)


class SqlAlchemyAccountUnitOfWork:
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
        # Sin *autoflush*: la consulta no debe adelantar al `SELECT` un conflicto que solo se
        # traduce en `commit` (lección de HU032).
        with self.session.no_autoflush:
            row = self.session.scalar(
                select(IdempotencyKeyRow)
                .where(IdempotencyKeyRow.key == key)
                .execution_options(populate_existing=True)
            )
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

    def lock_account(self, user_id: UUID) -> AccountDetails | None:
        row = self.session.scalar(
            select(UserAccountRow)
            .where(UserAccountRow.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            return None
        return AccountDetails(
            user_id=row.user_id,
            full_name=row.full_name,
            email=row.email,
            actor_type=ActorType(row.actor_type),
            status=AccountStatus(row.status),
            version=row.version,
            updated_at=row.updated_at,
        )

    def email_taken(self, email: str, user_id: UUID) -> bool:
        with self.session.no_autoflush:
            return bool(
                self.session.scalar(
                    select(
                        exists().where(
                            UserAccountRow.email == email, UserAccountRow.user_id != user_id
                        )
                    )
                )
            )

    def save_account(self, account: AccountDetails) -> None:
        try:
            self.session.execute(
                update(UserAccountRow)
                .where(UserAccountRow.user_id == account.user_id)
                .values(
                    full_name=account.full_name,
                    email=account.email,
                    version=account.version,
                    updated_at=account.updated_at,
                ),
                execution_options={"synchronize_session": False},
            )
        except DBAPIError as error:
            self.session.rollback()
            if _violated_constraint(error) == EMAIL_UNIQUE_CONSTRAINT:
                raise EmailAlreadyRegistered() from None
            raise AccountWriteFailed(error) from None

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
        except DBAPIError as error:
            self.session.rollback()
            constraint = _violated_constraint(error)
            if constraint == EMAIL_UNIQUE_CONSTRAINT:
                raise EmailAlreadyRegistered() from None
            if constraint == IDEMPOTENCY_PRIMARY_KEY:
                raise IdempotencyKeyConflict() from None
            raise AccountWriteFailed(error) from None
