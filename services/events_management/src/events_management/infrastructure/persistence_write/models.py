"""Modelos SQLAlchemy de la base de escritura (fuente de verdad y `outbox_event`).

Nunca se exponen como respuestas públicas. `organizer_id` es un identificador de
Usuarios: no hay claves foráneas hacia otra base.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sportapp_platform.database import new_metadata

IDEMPOTENCY_PRIMARY_KEY = "pk_command_idempotency"
REGISTRATION_UNIQUE = "uq_event_registration_event_id_athlete_id"
NOTIFICATION_DEDUPE_UNIQUE = "uq_notification_dedupe_key"
DEVICE_HASH_UNIQUE = "uq_push_token_token_hash"
DELIVERY_UNIQUE = "uq_notification_delivery_notification_id_token_id"


class WriteBase(DeclarativeBase):
    metadata = new_metadata()


class EventRow(WriteBase):
    """Agregado evento masivo (HU021); restricciones equivalentes a ADR-028. HU024
    (ADR-033): `published_at` existe si y solo si el evento está `PUBLISHED`. HU025
    (ADR-040): tarifa informativa opcional en `COP` entre 0.00 y 99999999.99."""

    __tablename__ = "event"
    __table_args__ = (
        CheckConstraint("status IN ('DRAFT', 'PUBLISHED')", name="status"),
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
        CheckConstraint("(status = 'PUBLISHED') = (published_at IS NOT NULL)", name="published_at"),
        CheckConstraint("(fee_amount IS NULL) = (fee_currency IS NULL)", name="fee"),
        CheckConstraint("fee_currency = 'COP'", name="fee_currency"),
        CheckConstraint("fee_amount BETWEEN 0 AND 99999999.99", name="fee_amount"),
        CheckConstraint("fee_amount IS NULL OR fee_updated_at IS NOT NULL", name="fee_updated_at"),
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
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fee_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    fee_currency: Mapped[str | None] = mapped_column(String(3))
    fee_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    # HU039 (ADR-045): `notificacion.solicitada.v1` se ordena por `recipient_id`, no por su
    # agregado; nulo = `aggregate_id` (todos los eventos anteriores).
    ordering_key: Mapped[str | None] = mapped_column(String(128))


class CommandIdempotencyRow(WriteBase):
    """Resultado de un comando reintentable por actor y clave, vigente durante 24 horas."""

    __tablename__ = "command_idempotency"

    actor_id: Mapped[UUID] = mapped_column(primary_key=True)
    # HU041: la clave se aísla también por operación (HU021 y HU041 del mismo actor).
    operation: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[UUID] = mapped_column(primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[UUID]
    response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class EventRegistrationRow(WriteBase):
    """Inscripción del deportista a un evento publicado (HU041, ADR-032)."""

    __tablename__ = "event_registration"
    __table_args__ = (
        UniqueConstraint("event_id", "athlete_id", name=REGISTRATION_UNIQUE),
        CheckConstraint("status IN ('REGISTERED')", name="status"),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint(
            "(applied_fee_amount IS NULL) = (applied_fee_currency IS NULL)", name="applied_fee"
        ),
    )

    registration_id: Mapped[UUID] = mapped_column(primary_key=True)
    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("event.event_id", ondelete="RESTRICT"), index=True
    )
    athlete_id: Mapped[UUID]
    status: Mapped[str] = mapped_column(String(16))
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    applied_fee_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    applied_fee_currency: Mapped[str | None] = mapped_column(String(3))
    version: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NotificationRow(WriteBase):
    """Notificación de la bandeja (HU039, ADR-045 §9): una por `dedupe_key`
    (`type:event_id:recipient_id`). `recipient_id` es un identificador de Usuarios."""

    __tablename__ = "notification"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name=NOTIFICATION_DEDUPE_UNIQUE),
        CheckConstraint("type IN ('EVENT_PUBLISHED_MATCH', 'EVENT_REMINDER_24H')", name="type"),
        CheckConstraint("priority IN ('NORMAL', 'HIGH')", name="priority"),
        CheckConstraint("char_length(title) BETWEEN 1 AND 120", name="title"),
        CheckConstraint("char_length(body) BETWEEN 1 AND 500", name="body"),
    )

    notification_id: Mapped[UUID] = mapped_column(primary_key=True)
    recipient_id: Mapped[UUID]
    type: Mapped[str] = mapped_column(String(32))
    priority: Mapped[str] = mapped_column(String(16))
    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("event.event_id", ondelete="RESTRICT"), index=True
    )
    title: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(String(500))
    dedupe_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NotificationPreferenceRow(WriteBase):
    """Preferencias del deportista (HU039): sin fila rigen los valores por defecto."""

    __tablename__ = "notification_preference"
    __table_args__ = (CheckConstraint("version >= 1", name="version"),)

    recipient_id: Mapped[UUID] = mapped_column(primary_key=True)
    event_matches: Mapped[bool] = mapped_column(Boolean)
    event_reminders: Mapped[bool] = mapped_column(Boolean)
    push_enabled: Mapped[bool] = mapped_column(Boolean)
    version: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PushTokenRow(WriteBase):
    """Token FCM del dispositivo (solo con `FCM_ENABLED`), cifrado en reposo con AES-GCM;
    `token_hash` (SHA-256) permite reasignarlo sin descifrar."""

    __tablename__ = "push_token"
    __table_args__ = (
        UniqueConstraint("token_hash", name=DEVICE_HASH_UNIQUE),
        CheckConstraint("platform IN ('android')", name="platform"),
    )

    token_id: Mapped[UUID] = mapped_column(primary_key=True)
    recipient_id: Mapped[UUID] = mapped_column(index=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    token_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    platform: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NotificationDeliveryRow(WriteBase):
    """Resultado del envío *push* por token (ADR-045 §10); nunca condiciona la bandeja."""

    __tablename__ = "notification_delivery"
    __table_args__ = (
        UniqueConstraint("notification_id", "token_id", name=DELIVERY_UNIQUE),
        CheckConstraint("channel IN ('FCM')", name="channel"),
        CheckConstraint("status IN ('SENT', 'FAILED')", name="status"),
        CheckConstraint("attempts >= 1", name="attempts"),
    )

    delivery_id: Mapped[UUID] = mapped_column(primary_key=True)
    notification_id: Mapped[UUID] = mapped_column(
        ForeignKey("notification.notification_id", ondelete="CASCADE")
    )
    token_id: Mapped[UUID] = mapped_column(ForeignKey("push_token.token_id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
