"""Unidades de trabajo SQLAlchemy de la base de escritura de entrenamientos (HU010)."""

from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from training_management.application.errors import AuthorizationConflict, IdempotencyKeyConflict
from training_management.application.ports import IdempotencyRecord, OutboxMessage
from training_management.domain.authorizations.model import (
    AuthorizationStatus,
    TrainingAuthorization,
)
from training_management.domain.plans.model import TrainingPlan
from training_management.infrastructure.persistence_write.models import (
    AUTHORIZATION_PRIMARY_KEY,
    IDEMPOTENCY_PRIMARY_KEY,
    IdempotencyKeyRow,
    OutboxEventRow,
    TrainingAuthorizationRow,
    TrainingPlanRow,
    TrainingPlanSessionRow,
)


def _violated_constraint(error: IntegrityError) -> str | None:
    diagnostics = getattr(error.orig, "diag", None)
    return getattr(diagnostics, "constraint_name", None)


class _SessionUnitOfWork:
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


class SqlAlchemyPlanningUnitOfWork(_SessionUnitOfWork):
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

    def has_active_authorization(self, athlete_id: UUID, professional_id: UUID) -> bool:
        status = self.session.scalar(
            select(TrainingAuthorizationRow.status)
            .where(
                TrainingAuthorizationRow.athlete_id == athlete_id,
                TrainingAuthorizationRow.professional_id == professional_id,
            )
            .with_for_update(read=True)
        )
        return status == AuthorizationStatus.ACTIVE.value

    def add_plan(self, plan: TrainingPlan) -> None:
        self.session.add(
            TrainingPlanRow(
                plan_id=plan.plan_id,
                athlete_id=plan.athlete_id,
                professional_id=plan.professional_id,
                title=plan.title,
                objectives=list(plan.objectives),
                start_date=plan.start_date,
                end_date=plan.end_date,
                status=plan.status.value,
                created_at=plan.created_at,
                updated_at=plan.created_at,
                version=plan.version,
            )
        )
        # El plan debe existir antes que sus sesiones (clave foránea local).
        self.session.flush()
        self.session.add_all(
            TrainingPlanSessionRow(
                plan_id=plan.plan_id,
                position=position,
                scheduled_date=session.scheduled_date,
                sport=session.sport.value,
                duration_minutes=session.duration_minutes,
                intensity_rpe=session.intensity_rpe,
            )
            for position, session in enumerate(plan.sessions)
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
            if _violated_constraint(error) == IDEMPOTENCY_PRIMARY_KEY:
                raise IdempotencyKeyConflict() from None
            raise


def _authorization(row: TrainingAuthorizationRow) -> TrainingAuthorization:
    return TrainingAuthorization(
        athlete_id=row.athlete_id,
        professional_id=row.professional_id,
        status=AuthorizationStatus(row.status),
        granted_at=row.granted_at,
        revoked_at=row.revoked_at,
        version=row.version,
    )


class SqlAlchemyAuthorizationUnitOfWork(_SessionUnitOfWork):
    def get_for_update(
        self, athlete_id: UUID, professional_id: UUID
    ) -> TrainingAuthorization | None:
        row = self.session.scalar(
            select(TrainingAuthorizationRow)
            .where(
                TrainingAuthorizationRow.athlete_id == athlete_id,
                TrainingAuthorizationRow.professional_id == professional_id,
            )
            .with_for_update()
        )
        return _authorization(row) if row is not None else None

    def add(self, authorization: TrainingAuthorization) -> None:
        self.session.add(
            TrainingAuthorizationRow(
                athlete_id=authorization.athlete_id,
                professional_id=authorization.professional_id,
                status=authorization.status.value,
                granted_at=authorization.granted_at,
                revoked_at=authorization.revoked_at,
                updated_at=authorization.revoked_at or authorization.granted_at,
                version=authorization.version,
            )
        )

    def update(self, authorization: TrainingAuthorization) -> None:
        row = self.session.get(
            TrainingAuthorizationRow, (authorization.athlete_id, authorization.professional_id)
        )
        if row is None:
            raise RuntimeError("La autorización a actualizar no está cargada.")
        row.status = authorization.status.value
        row.granted_at = authorization.granted_at
        row.revoked_at = authorization.revoked_at
        row.updated_at = authorization.revoked_at or authorization.granted_at
        row.version = authorization.version

    def commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            if _violated_constraint(error) == AUTHORIZATION_PRIMARY_KEY:
                raise AuthorizationConflict() from None
            raise
