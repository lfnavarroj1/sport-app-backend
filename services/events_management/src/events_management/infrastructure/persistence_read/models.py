"""Modelos SQLAlchemy de la base de lectura (proyecciones reconstruibles y `processed_event`).

Nunca se exponen como respuestas públicas.
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sportapp_platform.database import new_metadata

PROCESSED_EVENT_PRIMARY_KEY = "pk_processed_event"
EVENT_READ_PRIMARY_KEY = "pk_event_read"
REGISTRATION_READ_PRIMARY_KEY = "pk_event_registration_read"
INBOX_PRIMARY_KEY = "pk_notification_inbox_view"


class ReadBase(DeclarativeBase):
    metadata = new_metadata()


class EventReadRow(ReadBase):
    """Vista del evento (CMP-004) por estado completo desde `evento.creado.v1` y
    `evento.publicado.v1` (ADR-033) y `evento.tarifa_actualizada.v1` (HU025, ADR-040, con
    la tarifa). Un `DRAFT` solo es visible para su organizador; catálogo y calendario
    públicos lo excluyen. Sin contador de inscripciones (CMP-004)."""

    __tablename__ = "event_read"
    __table_args__ = (
        Index("ix_event_read_organizer_status_starts", "organizer_id", "status", "starts_at"),
        # HU035 (ADR-031): eventos publicados por periodo.
        Index("ix_event_read_status_starts", "status", "starts_at"),
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
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fee_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    fee_currency: Mapped[str | None] = mapped_column(String(3))
    fee_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    projected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EventRegistrationReadRow(ReadBase):
    """Inscripción proyectada (HU041) a partir de `usuario.inscrito.v1`; HU022 la usa para
    listar inscritos. Sin contador en `event_read` (clave caliente, CMP-004)."""

    __tablename__ = "event_registration_read"
    __table_args__ = (
        UniqueConstraint(
            "event_id", "athlete_id", name="uq_event_registration_read_event_id_athlete_id"
        ),
        Index("ix_event_registration_read_athlete_registered", "athlete_id", "registered_at"),
        # HU022: inscritos de un evento en orden `registered_at`, `registration_id` (keyset).
        Index(
            "ix_event_registration_read_event_order",
            "event_id",
            "registered_at",
            "registration_id",
        ),
    )

    registration_id: Mapped[UUID] = mapped_column(primary_key=True)
    event_id: Mapped[UUID]
    athlete_id: Mapped[UUID]
    status: Mapped[str] = mapped_column(String(16))
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    applied_fee_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    applied_fee_currency: Mapped[str | None] = mapped_column(String(3))
    version: Mapped[int] = mapped_column(BigInteger)
    projected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NotificationInboxViewRow(ReadBase):
    """Bandeja del deportista (HU039, ADR-045 §10) desde `notificacion.solicitada.v1`.

    `read_at` solo existe aquí: lo fija `POST /v1/me/notifications/{id}/read` y la
    proyección nunca lo sobrescribe (una entrega repetida no recrea la fila)."""

    __tablename__ = "notification_inbox_view"
    __table_args__ = (
        # Bandeja en orden `created_at DESC, notification_id DESC` (keyset).
        Index(
            "ix_notification_inbox_view_recipient_order",
            "recipient_id",
            "created_at",
            "notification_id",
        ),
    )

    notification_id: Mapped[UUID] = mapped_column(primary_key=True)
    recipient_id: Mapped[UUID]
    type: Mapped[str] = mapped_column(String(32))
    priority: Mapped[str] = mapped_column(String(16))
    event_id: Mapped[UUID]
    title: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    projected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProcessedEventRow(ReadBase):
    """Deduplicación del proyector por `event_id` del sobre (entrega al menos una vez)."""

    __tablename__ = "processed_event"

    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128))
    aggregate_id: Mapped[UUID]
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
