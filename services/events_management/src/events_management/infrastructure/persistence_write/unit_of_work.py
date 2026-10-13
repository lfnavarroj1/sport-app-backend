"""Unidad de trabajo SQLAlchemy de la creación de eventos (HU021)."""

from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from events_management.application.errors import IdempotencyKeyConflict
from events_management.application.ports import IdempotencyRecord, OutboxMessage
from events_management.domain.sport_events.model import SportEvent
from events_management.infrastructure.persistence_write.models import (
    IDEMPOTENCY_PRIMARY_KEY,
    CommandIdempotencyRow,
    EventRow,
    OutboxEventRow,
)


def _violated_constraint(error: IntegrityError) -> str | None:
    diagnostics = getattr(error.orig, "diag", None)
    return getattr(diagnostics, "constraint_name", None)


class SqlAlchemyEventCreationUnitOfWork:
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

    def find_idempotency(self, actor_id: UUID, key: UUID) -> IdempotencyRecord | None:
        row = self.session.get(CommandIdempotencyRow, (actor_id, key))
        if row is None:
            return None
        return IdempotencyRecord(
            actor_id=row.actor_id,
            key=row.key,
            operation=row.operation,
            request_fingerprint=row.request_fingerprint,
            resource_id=row.resource_id,
            response=row.response,
            created_at=row.created_at,
            expires_at=row.expires_at,
        )

    def delete_idempotency(self, actor_id: UUID, key: UUID) -> None:
        self.session.execute(
            delete(CommandIdempotencyRow).where(
                CommandIdempotencyRow.actor_id == actor_id, CommandIdempotencyRow.key == key
            )
        )

    def add_event(self, event: SportEvent) -> None:
        details = event.details
        self.session.add(
            EventRow(
                event_id=event.event_id,
                organizer_id=event.organizer_id,
                name=details.name,
                description=details.description,
                discipline=details.discipline,
                location_name=details.location.name,
                location_address=details.location.address,
                location_city=details.location.city,
                location_country_code=details.location.country_code,
                starts_at=details.starts_at,
                ends_at=details.ends_at,
                time_zone=details.time_zone,
                duration_minutes=details.duration_minutes,
                capacity=details.capacity,
                status=event.status.value,
                version=event.version,
                created_at=event.created_at,
                updated_at=event.updated_at,
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
            CommandIdempotencyRow(
                actor_id=record.actor_id,
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
