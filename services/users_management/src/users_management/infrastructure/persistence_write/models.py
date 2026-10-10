"""Modelos SQLAlchemy de la base de escritura (fuente de verdad y `outbox_event`).

Nunca se exponen como respuestas públicas.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sportapp_platform.database import new_metadata

EMAIL_UNIQUE_CONSTRAINT = "uq_user_account_email"
IDEMPOTENCY_PRIMARY_KEY = "pk_idempotency_key"


class WriteBase(DeclarativeBase):
    metadata = new_metadata()


class UserAccountRow(WriteBase):
    __tablename__ = "user_account"
    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('athlete', 'organizer', 'service_provider')", name="actor_type"
        ),
        CheckConstraint("status IN ('active')", name="status"),
    )

    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(254), unique=True)
    actor_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    password_hash: Mapped[str] = mapped_column(Text)
    accepted_policies_version: Mapped[str] = mapped_column(String(32))
    policies_accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(BigInteger)


class OutboxEventRow(WriteBase):
    """Outbox transaccional definida en CMP-001."""

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


class IdempotencyKeyRow(WriteBase):
    """Resultado de un comando reintentable, vigente durante 24 horas."""

    __tablename__ = "idempotency_key"

    key: Mapped[UUID] = mapped_column(primary_key=True)
    operation: Mapped[str] = mapped_column(String(64))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[UUID]
    response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
