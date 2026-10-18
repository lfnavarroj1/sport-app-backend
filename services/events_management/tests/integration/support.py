"""Bases de escritura y lectura de Eventos migradas, y casos de uso reales (HU021, HU024,
HU041)."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from events_management.api.payload import parse_event
from events_management.application.commands.create_event import (
    CreateEvent,
    CreateEventCommand,
)
from events_management.application.commands.publish_event import (
    PublishEvent,
    PublishEventCommand,
)
from events_management.application.commands.register_for_event import RegisterForEvent
from events_management.application.projections.event_projection import EventsProjector
from events_management.application.queries.my_events import ListMyEvents
from events_management.infrastructure.clock import SystemClock
from events_management.infrastructure.persistence_read.events import (
    SqlAlchemyEventDetailReader,
    SqlAlchemyEventReader,
    SqlAlchemyOwnEventReader,
    SqlAlchemyProjectionUnitOfWork,
    SqlAlchemyPublishedEventReader,
    SqlAlchemyRegistrationReader,
)
from events_management.infrastructure.persistence_read.models import EventReadRow
from events_management.infrastructure.persistence_write.models import EventRow
from events_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyEventCreationUnitOfWork,
    SqlAlchemyPublicationUnitOfWork,
    SqlAlchemyRegistrationUnitOfWork,
)
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.postgres import temporary_database

WRITE_HISTORY = "events_management.infrastructure.persistence_write:migrations"
READ_HISTORY = "events_management.infrastructure.persistence_read:migrations"


class OrganizerOnly:
    """`actor-permissions.v1` del organizador web: `events.create` y `events.publish`."""

    def allows(self, actor_type: str, permission: str) -> bool:
        return actor_type == "organizer" and permission in {"events.create", "events.publish"}


@dataclass
class EventDatabases:
    write_url: str
    read_url: str
    write_engine: Engine
    read_engine: Engine

    @property
    def writes(self) -> sessionmaker[Session]:
        return sessionmaker(self.write_engine, expire_on_commit=False)

    @property
    def reads(self) -> sessionmaker[Session]:
        return sessionmaker(self.read_engine, expire_on_commit=False)

    def create_event(self) -> CreateEvent:
        return CreateEvent(
            unit_of_work=lambda: SqlAlchemyEventCreationUnitOfWork(self.writes),
            permissions=OrganizerOnly(),
            clock=SystemClock(),
        )

    def publish(self) -> PublishEvent:
        return PublishEvent(
            unit_of_work=lambda: SqlAlchemyPublicationUnitOfWork(self.writes),
            permissions=OrganizerOnly(),
            clock=SystemClock(),
        )

    def my_events(self) -> ListMyEvents:
        return ListMyEvents(reader=SqlAlchemyOwnEventReader(self.reads))

    def published_reader(self) -> SqlAlchemyPublishedEventReader:
        return SqlAlchemyPublishedEventReader(self.reads)

    def project(self) -> EventsProjector:
        return EventsProjector(
            unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(self.reads), clock=SystemClock()
        )

    def register(self) -> RegisterForEvent:
        return RegisterForEvent(
            unit_of_work=lambda: SqlAlchemyRegistrationUnitOfWork(self.writes), clock=SystemClock()
        )

    def detail_reader(self) -> SqlAlchemyEventDetailReader:
        return SqlAlchemyEventDetailReader(self.reads)

    def registration_reader(self) -> SqlAlchemyRegistrationReader:
        return SqlAlchemyRegistrationReader(self.reads)

    def publish_event(
        self,
        *,
        capacity: int = 100,
        starts_in: timedelta = timedelta(days=30),
        status: str = "PUBLISHED",
    ) -> UUID:
        """Evento sintético sembrado en escritura y lectura (sin pasar por la publicación)."""
        event_id, now = uuid4(), datetime.now(UTC)
        starts_at = now + starts_in
        values: dict[str, Any] = {
            "event_id": event_id,
            "organizer_id": uuid4(),
            "name": "Carrera 10K Bogotá",
            "description": "Carrera recreativa de diez kilómetros para aficionados.",
            "discipline": "Running",
            "location_name": "Parque Metropolitano Simón Bolívar",
            "location_address": "Avenida Calle 63 # 60-50",
            "location_city": "Bogotá",
            "location_country_code": "CO",
            "starts_at": starts_at,
            "ends_at": starts_at + timedelta(hours=3),
            "time_zone": "America/Bogota",
            "duration_minutes": 180,
            "capacity": capacity,
            "status": status,
            "version": 2 if status == "PUBLISHED" else 1,
            "created_at": now,
            "updated_at": now,
            # La restricción de HU024: `published_at` existe si y solo si está publicado.
            "published_at": now if status == "PUBLISHED" else None,
        }
        with self.writes() as session, session.begin():
            session.add(EventRow(**values))
        with self.reads() as session, session.begin():
            session.add(EventReadRow(**values, projected_at=now))
        return event_id

    def reader(self) -> SqlAlchemyEventReader:
        return SqlAlchemyEventReader(self.reads)

    def count(self, model: Any, *, read: bool = False) -> int:
        with (self.reads if read else self.writes)() as session:
            return session.scalar(select(func.count()).select_from(model)) or 0


@contextmanager
def event_databases(prefix: str) -> Iterator[EventDatabases]:
    with (
        temporary_database(f"{prefix}_write") as write_url,
        temporary_database(f"{prefix}_read") as read_url,
    ):
        upgrade(WRITE_HISTORY, write_url)
        upgrade(READ_HISTORY, read_url)
        write_engine = create_engine(write_url, pool_size=10, max_overflow=10)
        read_engine = create_engine(read_url, pool_size=10, max_overflow=10)
        try:
            yield EventDatabases(write_url, read_url, write_engine, read_engine)
        finally:
            write_engine.dispose()
            read_engine.dispose()


def future_body(**overrides: Any) -> dict[str, Any]:
    """Evento válido sin depender de la fecha de ejecución (inicio en 2099)."""
    values: dict[str, Any] = {
        "name": "Carrera 10K Bogotá",
        "description": "Carrera recreativa de diez kilómetros para corredores aficionados.",
        "discipline": "Running",
        "location": {
            "name": "Parque Metropolitano Simón Bolívar",
            "address": "Avenida Calle 63 # 60-50",
            "city": "Bogotá",
            "country_code": "CO",
        },
        "starts_at": "2099-11-15T07:00:00-05:00",
        "time_zone": "America/Bogota",
        "duration_minutes": 180,
        "capacity": 5000,
    }
    values.update(overrides)
    return values


def event_command(key: UUID | None = None, **overrides: Any) -> CreateEventCommand:
    return CreateEventCommand(
        event=parse_event(future_body(**overrides)),
        idempotency_key=key or uuid4(),
        correlation_id=uuid4(),
    )


def publish_command(event_id: UUID, key: UUID | None = None) -> PublishEventCommand:
    return PublishEventCommand(
        event_id=event_id, idempotency_key=key or uuid4(), correlation_id=uuid4()
    )
