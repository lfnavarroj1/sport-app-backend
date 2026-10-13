"""Base de lectura de eventos masivos: transacción del proyector (CMP-004) y reglas de
visibilidad de `event_read`.

HU021 no expone consultas HTTP; las consultas de este módulo fijan la regla que deben
reutilizar las historias de consulta: un `DRAFT` solo lo ve su organizador y nunca
aparece en el catálogo ni en el calendario públicos (CA6).
"""

from datetime import datetime
from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from events_management.application.ports import (
    CalendarEventView,
    DuplicateDelivery,
    EventDetailView,
    EventSummary,
    PublishedEventsQuery,
    RegistrationsQuery,
    RegistrationView,
)
from events_management.domain.registrations.model import (
    AppliedFee,
    EventRegistration,
    RegistrationStatus,
)
from events_management.domain.sport_events.model import (
    EventDetails,
    EventStatus,
    Location,
    SportEvent,
)
from events_management.infrastructure.persistence_read.models import (
    EVENT_READ_PRIMARY_KEY,
    PROCESSED_EVENT_PRIMARY_KEY,
    REGISTRATION_READ_PRIMARY_KEY,
    EventReadRow,
    EventRegistrationReadRow,
    ProcessedEventRow,
)

# Estados que un catálogo o calendario público puede mostrar; `DRAFT` nunca lo es. Solo
# HU024 (pendiente) llevará eventos a `PUBLISHED`.
PUBLIC_STATUSES: frozenset[str] = frozenset({"PUBLISHED"})


def _event(row: EventReadRow) -> SportEvent:
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
    )


def _calendar(row: EventReadRow) -> CalendarEventView:
    return CalendarEventView(
        event_id=row.event_id,
        name=row.name,
        discipline=row.discipline,
        location_name=row.location_name,
        location_city=row.location_city,
        location_country_code=row.location_country_code,
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        time_zone=row.time_zone,
        status=row.status,
        version=row.version,
    )


def _violated_constraint(error: IntegrityError) -> str | None:
    diagnostics = getattr(error.orig, "diag", None)
    return getattr(diagnostics, "constraint_name", None)


class SqlAlchemyEventReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def owned_by(self, organizer_id: UUID, event_id: UUID) -> SportEvent | None:
        """Evento proyectado solo si pertenece al organizador autenticado."""
        with self._session_factory() as session:
            row = session.scalar(
                select(EventReadRow).where(
                    EventReadRow.event_id == event_id, EventReadRow.organizer_id == organizer_id
                )
            )
            return _event(row) if row is not None else None

    def public_events(self, limit: int = 100) -> list[CalendarEventView]:
        """Eventos visibles en catálogo y calendario públicos; excluye siempre `DRAFT`."""
        with self._session_factory() as session:
            rows = session.scalars(
                select(EventReadRow)
                .where(EventReadRow.status.in_(PUBLIC_STATUSES))
                .order_by(EventReadRow.starts_at, EventReadRow.event_id)
                .limit(limit)
            )
            return [_calendar(row) for row in rows]


class SqlAlchemyPublishedEventReader:
    """HU035: eventos publicados que se superponen con un periodo (ADR-031)."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def published(self, query: PublishedEventsQuery) -> list[CalendarEventView]:
        row = EventReadRow
        statement = select(row).where(
            row.status.in_(PUBLIC_STATUSES),
            row.starts_at <= query.starts_before_or_at,
            row.ends_at >= query.ends_at_or_after,
        )
        if query.after is not None:
            statement = statement.where(
                tuple_(row.starts_at, row.event_id)
                > tuple_(query.after.starts_at, query.after.event_id)
            )
        statement = statement.order_by(row.starts_at, row.event_id).limit(query.limit)
        with self._session_factory() as session:
            return [_calendar(item) for item in session.scalars(statement)]


def _registration(row: EventRegistrationReadRow) -> EventRegistration:
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
        created_at=row.registered_at,
        updated_at=row.registered_at,
    )


def _summary(row: EventReadRow) -> EventSummary:
    return EventSummary(
        name=row.name,
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        time_zone=row.time_zone,
        location_name=row.location_name,
        location_city=row.location_city,
        location_country_code=row.location_country_code,
    )


class SqlAlchemyEventDetailReader:
    """HU041: detalle de un evento publicado con la inscripción del deportista."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def published_detail(self, event_id: UUID, athlete_id: UUID | None) -> EventDetailView | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(EventReadRow).where(
                    EventReadRow.event_id == event_id, EventReadRow.status.in_(PUBLIC_STATUSES)
                )
            )
            if row is None:
                return None
            mine = (
                session.scalar(
                    select(EventRegistrationReadRow).where(
                        EventRegistrationReadRow.event_id == event_id,
                        EventRegistrationReadRow.athlete_id == athlete_id,
                    )
                )
                if athlete_id is not None
                else None
            )
            return EventDetailView(
                event=_calendar(row),
                description=row.description,
                capacity=row.capacity,
                my_registration=_registration(mine) if mine is not None else None,
            )


class SqlAlchemyRegistrationReader:
    """HU041: inscripciones propias enriquecidas con el evento proyectado."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list(self, query: RegistrationsQuery) -> list[RegistrationView]:
        registration, event = EventRegistrationReadRow, EventReadRow
        statement = (
            select(registration, event)
            .join(event, event.event_id == registration.event_id)
            .where(registration.athlete_id == query.athlete_id)
        )
        if query.status is not None:
            statement = statement.where(registration.status == query.status)
        if query.after is not None:
            statement = statement.where(
                tuple_(event.starts_at, registration.registration_id)
                > tuple_(query.after.starts_at, query.after.registration_id)
            )
        statement = statement.order_by(event.starts_at, registration.registration_id).limit(
            query.limit
        )
        with self._session_factory() as session:
            return [
                RegistrationView(registration=_registration(row), event=_summary(event_row))
                for row, event_row in session.execute(statement)
            ]


class SqlAlchemyProjectionUnitOfWork:
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

    def is_processed(self, event_id: UUID) -> bool:
        return self.session.get(ProcessedEventRow, event_id) is not None

    def projected_version(self, event_id: UUID) -> int | None:
        # Bloquea la vista existente: dos versiones del mismo evento se aplican en serie.
        version: int | None = self.session.scalar(
            select(EventReadRow.version).where(EventReadRow.event_id == event_id).with_for_update()
        )
        return version

    def add_event_view(self, event: SportEvent, projected_at: datetime) -> None:
        details = event.details
        self.session.add(
            EventReadRow(
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
                projected_at=projected_at,
            )
        )

    def registration_version(self, registration_id: UUID) -> int | None:
        version: int | None = self.session.scalar(
            select(EventRegistrationReadRow.version)
            .where(EventRegistrationReadRow.registration_id == registration_id)
            .with_for_update()
        )
        return version

    def add_registration_view(
        self, registration: EventRegistration, projected_at: datetime
    ) -> None:
        fee = registration.applied_fee
        self.session.add(
            EventRegistrationReadRow(
                registration_id=registration.registration_id,
                event_id=registration.event_id,
                athlete_id=registration.athlete_id,
                status=registration.status.value,
                registered_at=registration.registered_at,
                applied_fee_amount=fee.amount if fee else None,
                applied_fee_currency=fee.currency if fee else None,
                version=registration.version,
                projected_at=projected_at,
            )
        )

    def add_processed(
        self, message_id: UUID, event_type: str, aggregate_id: UUID, processed_at: datetime
    ) -> None:
        self.session.add(
            ProcessedEventRow(
                event_id=message_id,
                event_type=event_type,
                aggregate_id=aggregate_id,
                processed_at=processed_at,
            )
        )

    def commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            # Otra entrega del mismo mensaje (o del mismo evento nuevo) confirmó primero.
            if _violated_constraint(error) in {
                PROCESSED_EVENT_PRIMARY_KEY,
                EVENT_READ_PRIMARY_KEY,
                REGISTRATION_READ_PRIMARY_KEY,
            }:
                raise DuplicateDelivery() from None
            raise
