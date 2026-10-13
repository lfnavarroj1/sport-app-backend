"""Modelos SQLAlchemy de la base de escritura (fuente de verdad y `outbox_event`).

Nunca se exponen como respuestas públicas. `organizer_id` es un identificador de
Usuarios: no hay claves foráneas hacia otra base.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sportapp_platform.database import new_metadata

IDEMPOTENCY_PRIMARY_KEY = "pk_command_idempotency"


class WriteBase(DeclarativeBase):
    metadata = new_metadata()


class EventRow(WriteBase):
    """Agregado evento masivo (HU021); restricciones equivalentes a ADR-028."""

    __tablename__ = "event"
    __table_args__ = (
        CheckConstraint("status IN ('DRAFT')", name="status"),
        CheckConstraint("char_length(name) BETWEEN 3 AND 120", name="name"),
        CheckConstraint("char_length(description) BETWEEN 20 AND 2000", name="description"),
        CheckConstraint("char_length(discipline) BETWEEN 2 AND 80", name="discipline"),
        CheckConstraint("char_length(location_name) BETWEEN 3 AND 120", name="location_name"),
        CheckConstraint("char_length(location_address) BETWEEN 5 AND 200", name="location_address"),
        CheckConstraint("char_length(location_city) BETWEEN 2 AND 80", name="location_city"),
        CheckConstraint("location_country_code ~ '^[A-Z]{2}$'", name="location_country_code"),
        CheckConstraint("duration_minutes BETWEEN 15 AND 1440", name="duration_minutes"),
        CheckConstraint("capacity BETWEEN 1 AND 100000", name="capacity"),
        CheckConstraint(
            "ends_at = starts_at + make_interval(mins => duration_minutes)", name="ends_at"
        ),
        CheckConstraint("version >= 1", name="version"),
    )

    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    organizer_id: Mapped[UUID] = mapped_column(index=True)
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


class OutboxEventRow(WriteBase):
    """Outbox transaccional definida en CMP-002/CMP-004."""

    __tablename__ = "outbox_event"
    __table_args__ = (
        Index(
            "idx_outbox_pending",
            "next_attempt_at",
            "occurred_at",
            postgresql_where=text("published_at IS NULL"),
        ),
    )

    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[UUID]
    aggregate_version: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(128))
    schema_version: Mapped[int] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[UUID | None]
    causation_id: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(256))


class CommandIdempotencyRow(WriteBase):
    """Resultado de un comando reintentable por actor y clave, vigente durante 24 horas."""

    __tablename__ = "command_idempotency"

    actor_id: Mapped[UUID] = mapped_column(primary_key=True)
    key: Mapped[UUID] = mapped_column(primary_key=True)
    operation: Mapped[str] = mapped_column(String(64))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[UUID]
    response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
