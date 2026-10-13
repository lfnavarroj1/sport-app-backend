"""Modelos SQLAlchemy de la base de lectura (proyecciones reconstruibles y `processed_event`).

Nunca se exponen como respuestas públicas.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Index, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sportapp_platform.database import new_metadata

PROCESSED_EVENT_PRIMARY_KEY = "pk_processed_event"
EVENT_READ_PRIMARY_KEY = "pk_event_read"


class ReadBase(DeclarativeBase):
    metadata = new_metadata()


class EventReadRow(ReadBase):
    """Vista del evento (CMP-004) a partir de `evento.creado.v1`. Un `DRAFT` solo es
    visible para su organizador; catálogo y calendario públicos lo excluyen."""

    __tablename__ = "event_read"
    __table_args__ = (
        Index("ix_event_read_organizer_status_starts", "organizer_id", "status", "starts_at"),
    )

    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    organizer_id: Mapped[UUID]
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(2000))
    discipline: Mapped[str] = mapped_column(String(80))
    location_name: Mapped[str] = mapped_column(String(120))
    location_address: Mapped[str] = mapped_column(String(200))
    location_city: Mapped[str] = mapped_column(String(80))
    location_country_code: Mapped[str] = mapped_column(String(2))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    time_zone: Mapped[str] = mapped_column(String(64))
    duration_minutes: Mapped[int] = mapped_column(Integer)
    capacity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    projected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProcessedEventRow(ReadBase):
    """Deduplicación del proyector por `event_id` del sobre (entrega al menos una vez)."""

    __tablename__ = "processed_event"

    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128))
    aggregate_id: Mapped[UUID]
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
