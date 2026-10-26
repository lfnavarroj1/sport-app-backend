"""Modelos SQLAlchemy de la base única de Afiliación (ADR-024).

Nunca se exponen como respuestas públicas.
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
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sportapp_platform.database import new_metadata

INBOX_PRIMARY_KEY = "pk_inbox_event"
ACTIVE_SUBSCRIPTION_INDEX = "uq_subscription_active_user"
# HU016: clave de idempotencia por deportista, operación y clave.
COMMAND_IDEMPOTENCY_PRIMARY_KEY = "pk_command_idempotency"


class Base(DeclarativeBase):
    metadata = new_metadata()


class PlanRow(Base):
    """Plan del catálogo. Lo precarga la migración; no existen comandos para editarlo."""

    __tablename__ = "plan"
    __table_args__ = (CheckConstraint("status IN ('ACTIVE', 'DEFINED')", name="status"),)

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text)
    level: Mapped[int] = mapped_column(SmallInteger)
    display_order: Mapped[int] = mapped_column(SmallInteger)
    status: Mapped[str] = mapped_column(String(16))
    is_default_for_athlete: Mapped[bool] = mapped_column(Boolean)
    is_selectable: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PlanVersionRow(Base):
    """Versión de un plan. En `plans-2026-10-11.v1` los planes pagos tenían precio nulo
    (pendiente); desde `plans-2026-10-13.v1` sus precios están en `plan_offer`."""

    __tablename__ = "plan_version"

    plan_code: Mapped[str] = mapped_column(ForeignKey("plan.code"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    catalog_version: Mapped[str] = mapped_column(String(40))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    periodicity: Mapped[str | None] = mapped_column(String(16))
    terms: Mapped[str | None] = mapped_column(Text)


class PlanOfferRow(Base):
    """Oferta de precio de una versión de plan (ADR-036); la precarga `0003_plan_price_offers`.

    Precio de referencia en COP antes de impuestos: SportApp no cobra en el MVP.
    """

    __tablename__ = "plan_offer"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_code", "plan_version"], ["plan_version.plan_code", "plan_version.version"]
        ),
        UniqueConstraint("plan_code", "plan_version", "periodicity"),
        CheckConstraint(
            "periodicity IN ('NONE', 'MONTHLY', 'QUARTERLY', 'ANNUAL')", name="periodicity"
        ),
        CheckConstraint(
            "(periodicity = 'NONE' AND duration_months IS NULL)"
            " OR (periodicity = 'MONTHLY' AND duration_months = 1)"
            " OR (periodicity = 'QUARTERLY' AND duration_months = 3)"
            " OR (periodicity = 'ANNUAL' AND duration_months = 12)",
            name="duration",
        ),
        CheckConstraint("price >= 0", name="price"),
        CheckConstraint("currency = 'COP'", name="currency"),
    )

    offer_id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_code: Mapped[str] = mapped_column(String(32))
    plan_version: Mapped[int] = mapped_column(Integer)
    periodicity: Mapped[str] = mapped_column(String(16))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    duration_months: Mapped[int | None] = mapped_column(SmallInteger)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CapabilityRow(Base):
    __tablename__ = "capability"
    __table_args__ = (CheckConstraint("status IN ('ACTIVE')", name="status"),)

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    owner_domain: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))


class PlanVersionCapabilityRow(Base):
    """Capacidades materializadas por versión: la herencia no se calcula en ejecución."""

    __tablename__ = "plan_version_capability"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_code", "plan_version"], ["plan_version.plan_code", "plan_version.version"]
        ),
    )

    plan_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    plan_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    capability_code: Mapped[str] = mapped_column(ForeignKey("capability.code"), primary_key=True)
    limits: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class SubscriptionRow(Base):
    __tablename__ = "subscription"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_code", "plan_version"], ["plan_version.plan_code", "plan_version.version"]
        ),
        ForeignKeyConstraint(["plan_offer_id"], ["plan_offer.offer_id"]),
        # HU016 (ADR-036): el cambio termina la suscripción anterior (`ENDED`).
        CheckConstraint("status IN ('ACTIVE', 'ENDED')", name="status"),
        CheckConstraint("origin IN ('AUTO_REGISTRATION', 'USER_CHANGE')", name="origin"),
        CheckConstraint(
            "periodicity IS NULL OR periodicity IN ('NONE', 'MONTHLY', 'QUARTERLY', 'ANNUAL')",
            name="periodicity",
        ),
        CheckConstraint("(status = 'ENDED') = (ended_at IS NOT NULL)", name="ended"),
        # Nunca dos suscripciones activas para el mismo usuario (ADR-024).
        Index(
            ACTIVE_SUBSCRIPTION_INDEX,
            "user_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    subscription_id: Mapped[UUID] = mapped_column(primary_key=True)
    # Referencia externa a Usuarios: nunca una clave foránea entre bases.
    user_id: Mapped[UUID] = mapped_column(index=True)
    plan_code: Mapped[str] = mapped_column(String(32))
    plan_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    origin: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # HU016 (ADR-036): oferta aceptada y periodo; nulos solo en filas anteriores a `0004`.
    periodicity: Mapped[str | None] = mapped_column(String(16))
    plan_offer_id: Mapped[UUID | None]
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BillingCommitmentRow(Base):
    """Compromiso de pago **informativo** (ADR-036): SportApp no cobra ni concilia."""

    __tablename__ = "billing_commitment"
    __table_args__ = (
        ForeignKeyConstraint(["subscription_id"], ["subscription.subscription_id"]),
        ForeignKeyConstraint(["plan_offer_id"], ["plan_offer.offer_id"]),
        CheckConstraint("status IN ('PENDING_EXTERNAL', 'WAIVED')", name="status"),
        CheckConstraint("periodicity IN ('MONTHLY', 'QUARTERLY', 'ANNUAL')", name="periodicity"),
        CheckConstraint("amount >= 0", name="amount"),
        CheckConstraint("currency = 'COP'", name="currency"),
        CheckConstraint("period_end > period_start", name="period"),
        CheckConstraint("(status = 'WAIVED') = (waived_at IS NOT NULL)", name="waived"),
        Index("ix_billing_commitment_user_status", "user_id", "status"),
    )

    commitment_id: Mapped[UUID] = mapped_column(primary_key=True)
    subscription_id: Mapped[UUID]
    user_id: Mapped[UUID]
    plan_code: Mapped[str] = mapped_column(String(32))
    plan_version: Mapped[int] = mapped_column(Integer)
    plan_offer_id: Mapped[UUID]
    periodicity: Mapped[str] = mapped_column(String(16))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20))
    waived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SubscriptionHistoryRow(Base):
    """Movimientos del estado de cuenta (HU007 los lee), desde el alta `FREE`."""

    __tablename__ = "subscription_history"
    __table_args__ = (
        CheckConstraint(
            "movement_type IN ('FREE_ACTIVATED', 'PLAN_CHANGED', 'RETURNED_TO_FREE', "
            "'COMMITMENT_CREATED', 'COMMITMENT_WAIVED')",
            name="movement_type",
        ),
        CheckConstraint(
            "periodicity IN ('NONE', 'MONTHLY', 'QUARTERLY', 'ANNUAL')", name="periodicity"
        ),
        Index("ix_subscription_history_user", "user_id", "occurred_at", "movement_id"),
    )

    movement_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID]
    movement_type: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    subscription_id: Mapped[UUID] = mapped_column(ForeignKey("subscription.subscription_id"))
    commitment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("billing_commitment.commitment_id")
    )
    plan_code: Mapped[str] = mapped_column(String(32))
    periodicity: Mapped[str] = mapped_column(String(16))


class CommandIdempotencyRow(Base):
    """Resultado de `POST /v1/me/subscription/changes` por deportista y clave (24 h)."""

    __tablename__ = "command_idempotency"

    actor_id: Mapped[UUID] = mapped_column(primary_key=True)
    operation: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[UUID] = mapped_column(primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[UUID]
    response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class InboxEventRow(Base):
    """Deduplicación por `event_id` de los eventos consumidos y su resultado."""

    __tablename__ = "inbox_event"

    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128))
    user_id: Mapped[UUID]
    result: Mapped[str] = mapped_column(String(32))
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OutboxEventRow(Base):
    """Outbox transaccional de `afiliacion-outbox-publisher` (CMP-002)."""

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


class CatalogMetadataRow(Base):
    """Versión de catálogo instalada; la sonda de disponibilidad la compara con la esperada."""

    __tablename__ = "catalog_metadata"

    catalog_version: Mapped[str] = mapped_column(String(40), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Migración que instaló el catálogo (auditoría de la precarga; desde `0003`).
    migration_revision: Mapped[str | None] = mapped_column(String(32))
