"""Modelos SQLAlchemy de la base de escritura (fuente de verdad y `outbox_event`).

Nunca se exponen como respuestas públicas. `athlete_id` y `professional_id` son
identificadores de Usuarios: no hay claves foráneas hacia otra base.
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sportapp_platform.database import new_metadata

IDEMPOTENCY_PRIMARY_KEY = "pk_idempotency_key"
AUTHORIZATION_PRIMARY_KEY = "pk_training_authorization"


class WriteBase(DeclarativeBase):
    metadata = new_metadata()


class TrainingAuthorizationRow(WriteBase):
    """Autorización deportista → profesional (HU010)."""

    __tablename__ = "training_authorization"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'REVOKED')", name="status"),
        Index("ix_training_authorization_professional_id", "professional_id"),
    )

    athlete_id: Mapped[UUID] = mapped_column(primary_key=True)
    professional_id: Mapped[UUID] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(BigInteger)


class TrainingPlanRow(WriteBase):
    __tablename__ = "training_plan"
    __table_args__ = (
        CheckConstraint("status IN ('ASSIGNED')", name="status"),
        CheckConstraint("start_date <= end_date", name="period"),
    )

    plan_id: Mapped[UUID] = mapped_column(primary_key=True)
    athlete_id: Mapped[UUID] = mapped_column(index=True)
    professional_id: Mapped[UUID] = mapped_column(index=True)
    title: Mapped[str] = mapped_column(String(120))
    objectives: Mapped[list[str]] = mapped_column(JSONB)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(BigInteger)


class TrainingPlanSessionRow(WriteBase):
    __tablename__ = "training_plan_session"
    __table_args__ = (
        CheckConstraint(
            "sport IN ('running', 'cycling', 'swimming', 'strength', 'other')", name="sport"
        ),
        CheckConstraint("duration_minutes BETWEEN 1 AND 600", name="duration_minutes"),
        CheckConstraint("intensity_rpe BETWEEN 1 AND 10", name="intensity_rpe"),
    )

    plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("training_plan.plan_id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    scheduled_date: Mapped[date] = mapped_column(Date)
    sport: Mapped[str] = mapped_column(String(16))
    duration_minutes: Mapped[int] = mapped_column(SmallInteger)
    intensity_rpe: Mapped[int] = mapped_column(SmallInteger)


class OutboxEventRow(WriteBase):
    """Outbox transaccional definida en CMP-002/CMP-003."""

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
