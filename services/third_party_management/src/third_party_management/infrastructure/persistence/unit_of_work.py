"""Unidad de trabajo SQLAlchemy de la base única `terceros` (HU031).

Lo no confirmado explícitamente con `commit` se descarta. Las violaciones de unicidad que
el caso de uso sabe resolver se traducen a errores de aplicación: la clave de idempotencia,
la corrida pendiente única y la cuenta de Strava `ACTIVE` única.
"""

from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from third_party_management.application.errors import (
    AthleteAlreadyLinked,
    IdempotencyKeyConflict,
    SyncRunConflict,
)
from third_party_management.application.ports import (
    IdempotencyRecord,
    OAuthState,
    OutboxMessage,
)
from third_party_management.domain.activities import PROVIDER, ImportedActivity
from third_party_management.domain.connections import (
    AccountStatus,
    ExternalAccount,
    SealedTokens,
)
from third_party_management.domain.synchronization import (
    PENDING_STATUSES,
    SyncErrorCode,
    SyncRun,
    SyncStatus,
)
from third_party_management.infrastructure.persistence.models import (
    ACTIVE_ATHLETE_INDEX,
    ACTIVE_SYNC_RUN_INDEX,
    IDEMPOTENCY_PRIMARY_KEY,
    ExternalAccountRow,
    IdempotencyKeyRow,
    ImportedActivityRow,
    OAuthStateRow,
    OutboxEventRow,
    SyncRunRow,
)

_CONFLICTS: dict[str, type[Exception]] = {
    IDEMPOTENCY_PRIMARY_KEY: IdempotencyKeyConflict,
    ACTIVE_SYNC_RUN_INDEX: SyncRunConflict,
    ACTIVE_ATHLETE_INDEX: AthleteAlreadyLinked,
}
_PENDING = [status.value for status in PENDING_STATUSES]


def _violated_constraint(error: IntegrityError) -> str | None:
    diagnostics = getattr(error.orig, "diag", None)
    return getattr(diagnostics, "constraint_name", None)


def _utc(value: datetime | None) -> datetime | None:
    return value.astimezone(UTC) if value is not None else None


def external_account(row: ExternalAccountRow) -> ExternalAccount:
    tokens = None
    if (
        row.access_token_enc is not None
        and row.refresh_token_enc is not None
        and row.token_expires_at is not None
    ):
        tokens = SealedTokens(
            access=bytes(row.access_token_enc),
            refresh=bytes(row.refresh_token_enc),
            expires_at=row.token_expires_at.astimezone(UTC),
        )
    return ExternalAccount(
        user_id=row.user_id,
        external_athlete_id=row.external_athlete_id,
        tokens=tokens,
        scopes=tuple(row.scopes),
        consent_version=row.consent_version,
        status=AccountStatus(row.status),
        version=row.version,
        linked_at=row.linked_at.astimezone(UTC),
        revoked_at=_utc(row.revoked_at),
        created_at=row.created_at.astimezone(UTC),
        updated_at=row.updated_at.astimezone(UTC),
    )


def sync_run(row: SyncRunRow) -> SyncRun:
    return SyncRun(
        sync_run_id=row.sync_run_id,
        user_id=row.user_id,
        status=SyncStatus(row.status),
        imported_count=row.imported_count,
        error_code=SyncErrorCode(row.error_code) if row.error_code else None,
        requested_at=row.requested_at.astimezone(UTC),
        started_at=_utc(row.started_at),
        finished_at=_utc(row.finished_at),
        correlation_id=row.correlation_id,
    )


def imported_activity(row: ImportedActivityRow) -> ImportedActivity:
    return ImportedActivity(
        external_activity_id=row.external_activity_id,
        user_id=row.user_id,
        activity_id=row.activity_id,
        external_version=row.external_version,
        payload_hash=row.payload_hash,
        version=row.version,
        started_at=row.started_at.astimezone(UTC),
        imported_at=row.imported_at.astimezone(UTC),
        updated_at=row.updated_at.astimezone(UTC),
    )


class SqlAlchemyUnitOfWork:
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
        self.session.rollback()
        self.session.close()
        self._session = None

    # ------------------------------------------------------------ OAuth

    def add_oauth_state(self, state: OAuthState) -> None:
        self.session.add(
            OAuthStateRow(state=state.state, user_id=state.user_id, created_at=state.created_at)
        )

    def take_oauth_state(self, state: str) -> OAuthState | None:
        """Lee y borra en la misma transacción: un segundo callback ya no lo encuentra."""
        row = self.session.execute(
            delete(OAuthStateRow)
            .where(OAuthStateRow.state == state)
            .returning(OAuthStateRow.user_id, OAuthStateRow.created_at)
        ).first()
        if row is None:
            return None
        return OAuthState(state=state, user_id=row.user_id, created_at=row.created_at)

    def delete_oauth_states(self, user_id: UUID, created_before: datetime | None) -> None:
        statement = delete(OAuthStateRow).where(OAuthStateRow.user_id == user_id)
        if created_before is not None:
            statement = statement.where(OAuthStateRow.created_at < created_before)
        self.session.execute(statement)

    # ------------------------------------------------------- vinculación

    def get_account(self, user_id: UUID, *, for_update: bool = False) -> ExternalAccount | None:
        statement = select(ExternalAccountRow).where(ExternalAccountRow.user_id == user_id)
        if for_update:
            statement = statement.with_for_update()
        row = self.session.execute(statement).scalar_one_or_none()
        return external_account(row) if row is not None else None

    def active_owner_of(self, external_athlete_id: str) -> UUID | None:
        owner: UUID | None = self.session.execute(
            select(ExternalAccountRow.user_id).where(
                ExternalAccountRow.provider == PROVIDER,
                ExternalAccountRow.external_athlete_id == external_athlete_id,
                ExternalAccountRow.status == AccountStatus.ACTIVE.value,
            )
        ).scalar_one_or_none()
        return owner

    def save_account(self, account: ExternalAccount) -> None:
        tokens = account.tokens
        self.session.merge(
            ExternalAccountRow(
                user_id=account.user_id,
                provider=PROVIDER,
                external_athlete_id=account.external_athlete_id,
                access_token_enc=tokens.access if tokens else None,
                refresh_token_enc=tokens.refresh if tokens else None,
                token_expires_at=tokens.expires_at if tokens else None,
                scopes=list(account.scopes),
                consent_version=account.consent_version,
                status=account.status.value,
                version=account.version,
                linked_at=account.linked_at,
                revoked_at=account.revoked_at,
                created_at=account.created_at,
                updated_at=account.updated_at,
            )
        )

    # ----------------------------------------------------------- corridas

    def latest_run(self, user_id: UUID) -> SyncRun | None:
        row = self.session.execute(
            select(SyncRunRow)
            .where(SyncRunRow.user_id == user_id)
            .order_by(SyncRunRow.requested_at.desc(), SyncRunRow.sync_run_id.desc())
            .limit(1)
        ).scalar_one_or_none()
        return sync_run(row) if row is not None else None

    def get_run(self, sync_run_id: UUID) -> SyncRun | None:
        row = self.session.get(SyncRunRow, sync_run_id)
        return sync_run(row) if row is not None else None

    def pending_runs(self, user_id: UUID) -> list[SyncRun]:
        rows = self.session.execute(
            select(SyncRunRow)
            .where(SyncRunRow.user_id == user_id, SyncRunRow.status.in_(_PENDING))
            .with_for_update()
        ).scalars()
        return [sync_run(row) for row in rows]

    def add_run(self, run: SyncRun) -> None:
        self.session.add(
            SyncRunRow(
                sync_run_id=run.sync_run_id,
                user_id=run.user_id,
                status=run.status.value,
                imported_count=run.imported_count,
                error_code=run.error_code.value if run.error_code else None,
                requested_at=run.requested_at,
                started_at=run.started_at,
                finished_at=run.finished_at,
                correlation_id=run.correlation_id,
            )
        )

    def save_run(self, run: SyncRun) -> None:
        row = self.session.get(SyncRunRow, run.sync_run_id)
        if row is None:
            raise RuntimeError("La corrida no existe.")
        row.status = run.status.value
        row.imported_count = run.imported_count
        row.error_code = run.error_code.value if run.error_code else None
        row.finished_at = run.finished_at
        if not run.is_pending:
            row.lease_until = None

    def lock_owned_run(self, sync_run_id: UUID, worker_id: str) -> SyncRun | None:
        row = self.session.execute(
            select(SyncRunRow)
            .where(
                SyncRunRow.sync_run_id == sync_run_id,
                SyncRunRow.status == SyncStatus.RUNNING.value,
                SyncRunRow.worker_id == worker_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        return sync_run(row) if row is not None else None

    # ------------------------------------------------------- idempotencia

    def find_idempotency(
        self, user_id: UUID, operation: str, key: UUID
    ) -> IdempotencyRecord | None:
        # Sin autoflush: los conflictos deben aflorar en `commit`, donde se traducen.
        with self.session.no_autoflush:
            row = self.session.get(IdempotencyKeyRow, (user_id, operation, key))
        if row is None:
            return None
        return IdempotencyRecord(
            user_id=row.user_id,
            operation=row.operation,
            key=row.key,
            result_id=row.result_id,
            created_at=row.created_at.astimezone(UTC),
        )

    def delete_idempotency(self, user_id: UUID, operation: str, key: UUID) -> None:
        self.session.execute(
            delete(IdempotencyKeyRow).where(
                IdempotencyKeyRow.user_id == user_id,
                IdempotencyKeyRow.operation == operation,
                IdempotencyKeyRow.key == key,
            )
        )

    def add_idempotency(self, record: IdempotencyRecord) -> None:
        self.session.add(
            IdempotencyKeyRow(
                user_id=record.user_id,
                operation=record.operation,
                key=record.key,
                result_id=record.result_id,
                created_at=record.created_at,
            )
        )

    # ------------------------------------------- actividades y outbox

    def last_imported_start(self, user_id: UUID) -> datetime | None:
        value: datetime | None = self.session.execute(
            select(func.max(ImportedActivityRow.started_at)).where(
                ImportedActivityRow.user_id == user_id
            )
        ).scalar_one()
        return _utc(value)

    def get_imported(self, external_activity_id: str) -> ImportedActivity | None:
        row = self.session.execute(
            select(ImportedActivityRow)
            .where(
                ImportedActivityRow.provider == PROVIDER,
                ImportedActivityRow.external_activity_id == external_activity_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        return imported_activity(row) if row is not None else None

    def save_imported(self, activity: ImportedActivity, *, new: bool) -> None:
        if new:
            self.session.add(
                ImportedActivityRow(
                    provider=PROVIDER,
                    external_activity_id=activity.external_activity_id,
                    user_id=activity.user_id,
                    activity_id=activity.activity_id,
                    external_version=activity.external_version,
                    payload_hash=activity.payload_hash,
                    version=activity.version,
                    started_at=activity.started_at,
                    imported_at=activity.imported_at,
                    updated_at=activity.updated_at,
                )
            )
            # Una actividad repetida en la misma corrida debe encontrarse en la siguiente
            # consulta de esta transacción.
            self.session.flush()
            return
        row = self.session.get(ImportedActivityRow, (PROVIDER, activity.external_activity_id))
        if row is None:
            raise RuntimeError("La actividad importada no existe.")
        row.external_version = activity.external_version
        row.payload_hash = activity.payload_hash
        row.version = activity.version
        row.started_at = activity.started_at
        row.updated_at = activity.updated_at

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

    def commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            conflict = _CONFLICTS.get(_violated_constraint(error) or "")
            if conflict is not None:
                raise conflict() from None
            raise
