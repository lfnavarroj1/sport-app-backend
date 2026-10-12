"""Modelos SQLAlchemy de la base de lectura (proyecciones reconstruibles y `processed_event`).

Nunca se exponen como respuestas públicas.
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, Date, DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sportapp_platform.database import new_metadata

PROCESSED_EVENT_PRIMARY_KEY = "pk_processed_event"
PLAN_VIEW_PRIMARY_KEY = "pk_training_plan_view"


class ReadBase(DeclarativeBase):
    metadata = new_metadata()


class TrainingPlanViewRow(ReadBase):
    """Vista del plan asignado (CA4); la construye `entrenamientos-read-projector`."""

    __tablename__ = "training_plan_view"

    plan_id: Mapped[UUID] = mapped_column(primary_key=True)
    athlete_id: Mapped[UUID] = mapped_column(index=True)
    professional_id: Mapped[UUID] = mapped_column(index=True)
    title: Mapped[str] = mapped_column(String(120))
    objectives: Mapped[list[str]] = mapped_column(JSONB)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(BigInteger)
    sessions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    projected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProcessedEventRow(ReadBase):
    """Deduplicación del proyector por `event_id` (entrega al menos una vez)."""

    __tablename__ = "processed_event"

    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128))
    aggregate_id: Mapped[UUID]
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
