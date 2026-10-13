"""Modelos SQLAlchemy de la base única del dominio.

Nunca se exponen como respuestas públicas. `provider_id` es un identificador de
Usuarios: no hay claves foráneas hacia otra base.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    SmallInteger,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sportapp_platform.database import new_metadata

IDEMPOTENCY_PRIMARY_KEY = "pk_idempotency_key"


class Base(DeclarativeBase):
    metadata = new_metadata()


class ServiceRow(Base):
    """Servicio y su tarifa de referencia (ADR-027); sin datos de pago."""

    __tablename__ = "service"
    __table_args__ = (
        CheckConstraint(
            "category IN ('SPORTS_MEDICINE', 'COACHING', 'NUTRITION', 'TRANSPORT', "
            "'ACCOMPANIMENT')",
            name="category",
        ),
        CheckConstraint("modality IN ('IN_PERSON', 'VIRTUAL', 'HYBRID')", name="modality"),
        CheckConstraint("currency = 'COP'", name="currency"),
        CheckConstraint(
            "billing_unit IN ('SERVICE', 'SESSION', 'HOUR', 'DAY')", name="billing_unit"
        ),
        CheckConstraint("price_amount BETWEEN 0 AND 999999999.99", name="price_amount"),
        CheckConstraint("status IN ('DRAFT', 'PUBLISHED')", name="status"),
        CheckConstraint("char_length(name) BETWEEN 3 AND 120", name="name"),
        CheckConstraint("char_length(description) BETWEEN 20 AND 2000", name="description"),
    )

    service_id: Mapped[UUID] = mapped_column(primary_key=True)
    provider_id: Mapped[UUID] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(2000))
    category: Mapped[str] = mapped_column(String(32))
    modality: Mapped[str] = mapped_column(String(16))
    price_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    billing_unit: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), index=True)
    version: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ServiceAvailabilityRow(Base):
    """Intervalo explícito `[starts_at, ends_at)` en UTC con la zona declarada."""

    __tablename__ = "service_availability"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="interval"),
        CheckConstraint("ends_at - starts_at <= interval '24 hours'", name="duration"),
        CheckConstraint("capacity BETWEEN 1 AND 100", name="capacity"),
    )

    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("service.service_id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    time_zone: Mapped[str] = mapped_column(String(64))
    capacity: Mapped[int] = mapped_column(SmallInteger)


class IdempotencyKeyRow(Base):
    """Resultado de un comando reintentable por profesional, vigente durante 24 horas."""

    __tablename__ = "idempotency_key"

    provider_id: Mapped[UUID] = mapped_column(primary_key=True)
    key: Mapped[UUID] = mapped_column(primary_key=True)
    operation: Mapped[str] = mapped_column(String(64))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[UUID]
    response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
