"""Modelos SQLAlchemy de la base de lectura (proyecciones reconstruibles y `processed_event`).

Nunca se exponen como respuestas públicas.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sportapp_platform.database import new_metadata

PROCESSED_EVENT_PRIMARY_KEY = "pk_processed_event"
PLAN_VIEW_PRIMARY_KEY = "pk_training_plan_view"
AUTHORIZATION_VIEW_PRIMARY_KEY = "pk_training_authorization_view"
SESSION_HISTORY_PRIMARY_KEY = "pk_training_session_history"
METRIC_RESULT_ORDER_INDEX = "ix_training_metric_result_athlete_order"


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


class TrainingAuthorizationViewRow(ReadBase):
    """Autorización deportista → profesional proyectada (HU011, CA3)."""

    __tablename__ = "training_authorization_view"
    __table_args__ = (
        # CA5: deportistas vigentes de un profesional en orden `granted_at`, `athlete_id` desc.
        Index(
            "ix_training_authorization_view_professional_order",
            "professional_id",
            "status",
            "granted_at",
            "athlete_id",
        ),
    )

    athlete_id: Mapped[UUID] = mapped_column(primary_key=True)
    professional_id: Mapped[UUID] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(BigInteger)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    projected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TrainingSessionHistoryRow(ReadBase):
    """Sesión terminada (HU011) a partir de `entrenamiento.finalizado.v1`; sin datos de salud."""

    __tablename__ = "training_session_history"
    __table_args__ = (
        # Orden estable del historial y del cursor: `ended_at` desc, `session_id` desc.
        Index("ix_training_session_history_athlete_order", "athlete_id", "ended_at", "session_id"),
    )

    session_id: Mapped[UUID] = mapped_column(primary_key=True)
    athlete_id: Mapped[UUID]
    plan_id: Mapped[UUID | None]
    sport: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(BigInteger)
    projected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TrainingPlanSessionViewRow(ReadBase):
    """Sesión planificada de un plan (HU035) a partir de `plan.entrenamiento_creado.v1`."""

    __tablename__ = "training_plan_session_view"
    __table_args__ = (
        # Orden y cursor del calendario: deportista, fecha, plan y sesión.
        Index(
            "ix_training_plan_session_view_calendar",
            "athlete_id",
            "scheduled_date",
            "plan_id",
            "session_id",
        ),
    )

    session_id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    athlete_id: Mapped[UUID]
    plan_title: Mapped[str] = mapped_column(String(120))
    plan_status: Mapped[str] = mapped_column(String(16))
    plan_version: Mapped[int] = mapped_column(BigInteger)
    position: Mapped[int] = mapped_column(SmallInteger)
    scheduled_date: Mapped[date] = mapped_column(Date)
    sport: Mapped[str] = mapped_column(String(16))
    duration_minutes: Mapped[int] = mapped_column(SmallInteger)
    intensity_rpe: Mapped[int] = mapped_column(SmallInteger)
    projected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ActiveTrainingSessionRow(ReadBase):
    """Sesión activa (HU032) a partir de `entrenamiento.iniciado.v1`; la elimina
    `entrenamiento.finalizado.v1`. Sin datos de salud ni ubicación."""

    __tablename__ = "active_training_session"

    session_id: Mapped[UUID] = mapped_column(primary_key=True)
    athlete_id: Mapped[UUID] = mapped_column(index=True)
    plan_id: Mapped[UUID | None]
    sport: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(BigInteger)
    projected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TrainingMetricResultRow(ReadBase):
    """Resultado de un indicador (HU034) a partir de `indicador.calculado.v1`; reconstruible."""

    __tablename__ = "training_metric_result"
    __table_args__ = (
        # Orden estable del historial y del cursor: `measured_at` desc, `calculation_id` desc.
        Index(
            METRIC_RESULT_ORDER_INDEX,
            "athlete_id",
            text("measured_at DESC"),
            text("calculation_id DESC"),
        ),
    )

    calculation_id: Mapped[UUID] = mapped_column(primary_key=True)
    athlete_id: Mapped[UUID]
    metric_code: Mapped[str] = mapped_column(String(64))
    method_code: Mapped[str] = mapped_column(String(64))
    method_version: Mapped[int] = mapped_column(Integer)
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    inputs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    result_value: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    result_unit: Mapped[str] = mapped_column(String(32))
    result_scale: Mapped[int] = mapped_column(SmallInteger)
    status: Mapped[str] = mapped_column(String(16))
    disclaimer_key: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(BigInteger)
    projected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProcessedEventRow(ReadBase):
    """Deduplicación del proyector por `event_id` (entrega al menos una vez)."""

    __tablename__ = "processed_event"

    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128))
    aggregate_id: Mapped[UUID]
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
