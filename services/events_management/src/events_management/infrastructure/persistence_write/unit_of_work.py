"""Unidades de trabajo SQLAlchemy de la base de escritura de eventos (HU021, HU024, HU025,
HU041)."""

from collections.abc import Mapping
from types import TracebackType
from typing import ClassVar, Self
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from events_management.application.errors import IdempotencyKeyConflict, RegistrationConflict
from events_management.application.ports import EventSummary, IdempotencyRecord, OutboxMessage
from events_management.domain.registrations.model import (
    AppliedFee,
    EventForRegistration,
    EventRegistration,
    RegistrationStatus,
)
from events_management.domain.sport_events.fee import EventFee
from events_management.domain.sport_events.model import (
    EventDetails,
    EventStatus,
    Location,
    SportEvent,
)
from events_management.infrastructure.persistence_write.models import (
    IDEMPOTENCY_PRIMARY_KEY,
    REGISTRATION_UNIQUE,
    CommandIdempotencyRow,
    EventRegistrationRow,
    EventRow,
    OutboxEventRow,
)


def _violated_constraint(error: IntegrityError) -> str | None:
    diagnostics = getattr(error.orig, "diag", None)
    return getattr(diagnostics, "constraint_name", None)


class _WriteUnitOfWork:
    """Sesión, idempotencia por actor y operación, y outbox comunes a los comandos."""

    _conflicts: ClassVar[Mapping[str | None, type[Exception]]] = {
        IDEMPOTENCY_PRIMARY_KEY: IdempotencyKeyConflict
    }

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

    def find_idempotency(
        self, actor_id: UUID, operation: str, key: UUID
    ) -> IdempotencyRecord | None:
        # Sin autoflush: los conflictos deben aflorar en `commit`, donde se traducen.
        with self.session.no_autoflush:
            row = self.session.get(CommandIdempotencyRow, (actor_id, operation, key))
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

    def delete_idempotency(self, actor_id: UUID, operation: str, key: UUID) -> None:
        self.session.execute(
            delete(CommandIdempotencyRow).where(
                CommandIdempotencyRow.actor_id == actor_id,
                CommandIdempotencyRow.operation == operation,
                CommandIdempotencyRow.key == key,
            )
        )

    def add_idempotency(self, record: IdempotencyRecord) -> None:
        self.session.add(
            CommandIdempotencyRow(
                actor_id=record.actor_id,
                operation=record.operation,
                key=record.key,
                request_fingerprint=record.request_fingerprint,
                resource_id=record.resource_id,
                response=record.response,
                created_at=record.created_at,
                expires_at=record.expires_at,
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

    def commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            conflict = self._conflicts.get(_violated_constraint(error))
            if conflict is not None:
                raise conflict() from None
            raise


class SqlAlchemyEventCreationUnitOfWork(_WriteUnitOfWork):
    """HU021: evento en borrador, idempotencia y `evento.creado.v1`."""

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
                published_at=event.published_at,
            )
        )


def _event_fee(row: EventRow) -> EventFee | None:
    if row.fee_amount is None or row.fee_currency is None:
        return None
    return EventFee(amount=row.fee_amount, currency=row.fee_currency)


def registration_count(session: Session, event_id: UUID) -> int:
    """Inscripciones del evento en la base de escritura (HU041 y HU025)."""
    count: int = (
        session.scalar(
            select(func.count())
            .select_from(EventRegistrationRow)
            .where(EventRegistrationRow.event_id == event_id)
        )
        or 0
    )
    return count


def _sport_event(row: EventRow) -> SportEvent:
    return SportEvent(
        event_id=row.event_id,
        organizer_id=row.organizer_id,
        details=EventDetails(
            name=row.name,
            description=row.description,
            discipline=row.discipline,
            location=Location(
                name=row.location_name,
                address=row.location_address,
                city=row.location_city,
                country_code=row.location_country_code,
            ),
            starts_at=row.starts_at,
            ends_at=row.ends_at,
            time_zone=row.time_zone,
            duration_minutes=row.duration_minutes,
            capacity=row.capacity,
        ),
        status=EventStatus(row.status),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        published_at=row.published_at,
        fee=_event_fee(row),
        fee_updated_at=row.fee_updated_at,
    )


class SqlAlchemyPublicationUnitOfWork(_WriteUnitOfWork):
    """HU024: evento bloqueado, transición `DRAFT` → `PUBLISHED`, idempotencia y
    `evento.publicado.v1`."""

    def lock_event(self, event_id: UUID) -> SportEvent | None:
        row = self.session.scalar(
            select(EventRow).where(EventRow.event_id == event_id).with_for_update()
        )
        return _sport_event(row) if row is not None else None

    def update_event(self, event: SportEvent) -> None:
        row = self.session.get(EventRow, event.event_id)
        if row is None:
            raise RuntimeError("El evento bloqueado no está cargado.")
        row.status = event.status.value
        row.version = event.version
        row.updated_at = event.updated_at
        row.published_at = event.published_at


class SqlAlchemyFeeUnitOfWork(SqlAlchemyPublicationUnitOfWork):
    """HU025: evento bloqueado, tarifa, versión, idempotencia y
    `evento.tarifa_actualizada.v1`."""

    def update_fee(self, event: SportEvent) -> None:
        row = self.session.get(EventRow, event.event_id)
        if row is None:
            raise RuntimeError("El evento bloqueado no está cargado.")
        fee = event.fee
        row.fee_amount = fee.amount if fee else None
        row.fee_currency = fee.currency if fee else None
        row.fee_updated_at = event.fee_updated_at
        row.version = event.version
        row.updated_at = event.updated_at

    def count_registrations(self, event_id: UUID) -> int:
        return registration_count(self.session, event_id)


def _registration(row: EventRegistrationRow) -> EventRegistration:
    fee = (
        AppliedFee(row.applied_fee_amount, row.applied_fee_currency)
        if row.applied_fee_amount is not None and row.applied_fee_currency is not None
        else None
    )
    return EventRegistration(
        registration_id=row.registration_id,
        event_id=row.event_id,
        athlete_id=row.athlete_id,
        status=RegistrationStatus(row.status),
        registered_at=row.registered_at,
        applied_fee=fee,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyRegistrationUnitOfWork(_WriteUnitOfWork):
    """HU041: evento bloqueado, inscripción, idempotencia y `usuario.inscrito.v1`."""

    _conflicts: ClassVar[Mapping[str | None, type[Exception]]] = {
        IDEMPOTENCY_PRIMARY_KEY: IdempotencyKeyConflict,
        REGISTRATION_UNIQUE: RegistrationConflict,
    }

    def lock_event(self, event_id: UUID) -> EventForRegistration | None:
        row = self.session.scalar(
            select(EventRow).where(EventRow.event_id == event_id).with_for_update()
        )
        if row is None:
            return None
        # HU025: la tarifa vigente se conserva como `applied_fee` de la inscripción nueva.
        fee = _event_fee(row)
        return EventForRegistration(
            event_id=row.event_id,
            status=EventStatus(row.status),
            starts_at=row.starts_at,
            capacity=row.capacity,
            fee=AppliedFee(fee.amount, fee.currency) if fee else None,
        )

    def event_summary(self, event_id: UUID) -> EventSummary:
        row = self.session.get(EventRow, event_id)
        if row is None:
            raise RuntimeError("El evento bloqueado no está cargado.")
        return EventSummary(
            name=row.name,
            starts_at=row.starts_at,
            ends_at=row.ends_at,
            time_zone=row.time_zone,
            location_name=row.location_name,
            location_city=row.location_city,
            location_country_code=row.location_country_code,
        )

    def count_registrations(self, event_id: UUID) -> int:
        return registration_count(self.session, event_id)

    def find_registration(self, event_id: UUID, athlete_id: UUID) -> EventRegistration | None:
        row = self.session.scalar(
            select(EventRegistrationRow).where(
                EventRegistrationRow.event_id == event_id,
                EventRegistrationRow.athlete_id == athlete_id,
            )
        )
        return _registration(row) if row is not None else None

    def add_registration(self, registration: EventRegistration) -> None:
        fee = registration.applied_fee
        self.session.add(
            EventRegistrationRow(
                registration_id=registration.registration_id,
                event_id=registration.event_id,
                athlete_id=registration.athlete_id,
                status=registration.status.value,
                registered_at=registration.registered_at,
                applied_fee_amount=fee.amount if fee else None,
                applied_fee_currency=fee.currency if fee else None,
                version=registration.version,
                created_at=registration.created_at,
                updated_at=registration.updated_at,
            )
        )
