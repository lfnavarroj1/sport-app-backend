"""Modelos SQLAlchemy de la base de escritura (fuente de verdad y `outbox_event`).

Nunca se exponen como respuestas públicas. `athlete_id` y `professional_id` son
identificadores de Usuarios: no hay claves foráneas hacia otra base.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    ForeignKeyConstraint,
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

IDEMPOTENCY_PRIMARY_KEY = "pk_idempotency_key"
COMMAND_IDEMPOTENCY_PRIMARY_KEY = "pk_command_idempotency"
TRAINING_SESSION_PRIMARY_KEY = "pk_training_session"
ACTIVE_SESSION_INDEX = "uq_training_session_athlete_active"
AUTHORIZATION_PRIMARY_KEY = "pk_training_authorization"
METRIC_CALCULATION_PRIMARY_KEY = "pk_training_metric_calculation"


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


class TrainingSessionRow(WriteBase):
    """Sesión de entrenamiento de HU032 (ADR-029); `plan_id` opcional de HU010."""

    __tablename__ = "training_session"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'FINISHED')", name="status"),
        CheckConstraint(
            "sport IN ('running', 'cycling', 'swimming', 'strength', 'other')", name="sport"
        ),
        CheckConstraint(
            "(status = 'ACTIVE' AND ended_at IS NULL AND duration_seconds IS NULL AND version = 1)"
            " OR (status = 'FINISHED' AND ended_at >= started_at AND duration_seconds >= 0"
            " AND version = 2)",
            name="transition",
        ),
        # Como máximo una sesión activa por deportista: serializa los inicios concurrentes.
        Index(
            ACTIVE_SESSION_INDEX,
            "athlete_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    session_id: Mapped[UUID] = mapped_column(primary_key=True)
    athlete_id: Mapped[UUID] = mapped_column(index=True)
    plan_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("training_plan.plan_id", ondelete="RESTRICT")
    )
    sport: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CommandIdempotencyRow(WriteBase):
    """Resultado de un comando de HU032 o HU034 por deportista y operación, vigente 30 días."""

    __tablename__ = "command_idempotency"

    actor_id: Mapped[UUID] = mapped_column(primary_key=True)
    operation: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[UUID] = mapped_column(primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[UUID]
    response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class TrainingMetricCalculationRow(WriteBase):
    """Cálculo inmutable de un indicador de HU034 (ADR-030); valores con escala explícita."""

    __tablename__ = "training_metric_calculation"
    __table_args__ = (
        CheckConstraint("status IN ('CONFIRMED')", name="status"),
        CheckConstraint("version = 1", name="version"),
        CheckConstraint("method_version >= 1", name="method_version"),
        CheckConstraint("result_scale BETWEEN 0 AND 4", name="result_scale"),
    )

    calculation_id: Mapped[UUID] = mapped_column(primary_key=True)
    athlete_id: Mapped[UUID] = mapped_column(index=True)
    metric_code: Mapped[str] = mapped_column(String(64))
    method_code: Mapped[str] = mapped_column(String(64))
    method_version: Mapped[int] = mapped_column(Integer)
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    result_value: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    result_unit: Mapped[str] = mapped_column(String(32))
    result_scale: Mapped[int] = mapped_column(SmallInteger)
    status: Mapped[str] = mapped_column(String(16))
    disclaimer_key: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(BigInteger)
    data_fingerprint: Mapped[str] = mapped_column(String(64))


class TrainingMetricInputRow(WriteBase):
    """Entrada normalizada de un cálculo de HU034, en el orden del catálogo."""

    __tablename__ = "training_metric_input"

    calculation_id: Mapped[UUID] = mapped_column(
        # Nombre explícito: el de la convención supera los 63 caracteres de PostgreSQL.
        ForeignKey(
            "training_metric_calculation.calculation_id",
            ondelete="CASCADE",
            name="fk_training_metric_input_calculation",
        ),
        primary_key=True,
    )
    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    position: Mapped[int] = mapped_column(SmallInteger)
    value: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    unit: Mapped[str] = mapped_column(String(32))


class FoodCatalogItemRow(WriteBase):
    """Alimento del catálogo cerrado de HU014/HU043 (ADR-037); lo precarga la migración."""

    __tablename__ = "food_catalog_item"

    food_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    label_key: Mapped[str] = mapped_column(String(80))
    category: Mapped[str] = mapped_column(String(16))
    default_unit: Mapped[str] = mapped_column(String(8))
    allowed_units: Mapped[list[str]] = mapped_column(JSONB)
    allergens: Mapped[list[str]] = mapped_column(JSONB)
    incompatible_with: Mapped[list[str]] = mapped_column(JSONB)
    catalog_version: Mapped[str] = mapped_column(String(64))


class NutritionPlanRow(WriteBase):
    """Plan de alimentación asignado (HU014); inmutable en versión 1."""

    __tablename__ = "nutrition_plan"
    __table_args__ = (
        CheckConstraint("status IN ('ASSIGNED')", name="status"),
        CheckConstraint("version = 1", name="version"),
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
    catalog_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(BigInteger)


class NutritionPlanMealRow(WriteBase):
    """Comida del menú diario del plan, en el orden del profesional."""

    __tablename__ = "nutrition_plan_meal"
    __table_args__ = (
        CheckConstraint(
            "meal_type IN ('BREAKFAST', 'LUNCH', 'DINNER', 'SNACK', 'HYDRATION')",
            name="meal_type",
        ),
    )

    plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("nutrition_plan.plan_id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    meal_type: Mapped[str] = mapped_column(String(16))


class NutritionPlanItemRow(WriteBase):
    """Alimento de una comida: cantidad con un decimal y unidad permitida del catálogo."""

    __tablename__ = "nutrition_plan_item"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "meal_position"],
            ["nutrition_plan_meal.plan_id", "nutrition_plan_meal.position"],
            ondelete="CASCADE",
        ),
        CheckConstraint("quantity BETWEEN 0.1 AND 5000.0", name="quantity"),
        CheckConstraint("unit IN ('G', 'ML', 'UNIT', 'CUP', 'TBSP')", name="unit"),
    )

    plan_id: Mapped[UUID] = mapped_column(primary_key=True)
    meal_position: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    position: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    food_code: Mapped[str] = mapped_column(
        String(64), ForeignKey("food_catalog_item.food_code", ondelete="RESTRICT")
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(5, 1))
    unit: Mapped[str] = mapped_column(String(8))


class RouteRow(WriteBase):
    """Ruta sugerible publicada por un organizador (HU026, ADR-041); al retirarse se conserva.

    `organizer_id` es un identificador de Usuarios, sin clave foránea entre bases.
    """

    __tablename__ = "route"
    __table_args__ = (
        CheckConstraint("status IN ('PUBLISHED', 'RETIRED')", name="status"),
        CheckConstraint(
            "sport IN ('running', 'cycling', 'swimming', 'strength', 'other')", name="sport"
        ),
        CheckConstraint("difficulty IN ('LOW', 'MODERATE', 'HIGH')", name="difficulty"),
        CheckConstraint("distance_km BETWEEN 0.1 AND 500.0", name="distance_km"),
        CheckConstraint("start_lat BETWEEN -90 AND 90", name="start_lat"),
        CheckConstraint("start_lng BETWEEN -180 AND 180", name="start_lng"),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint("(status = 'RETIRED') = (retired_at IS NOT NULL)", name="retired_at"),
    )

    route_id: Mapped[UUID] = mapped_column(primary_key=True)
    organizer_id: Mapped[UUID] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(String(2000))
    sport: Mapped[str] = mapped_column(String(16))
    distance_km: Mapped[Decimal] = mapped_column(Numeric(4, 1))
    difficulty: Mapped[str] = mapped_column(String(16))
    city: Mapped[str] = mapped_column(String(80))
    country_code: Mapped[str] = mapped_column(String(2))
    start_lat: Mapped[float] = mapped_column(Double)
    start_lng: Mapped[float] = mapped_column(Double)
    # Hasta 200 puntos `{lat, lng}` en el orden del organizador.
    waypoints: Mapped[list[dict[str, float]]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TrainingPlanTemplateRow(WriteBase):
    """Plantilla de plan publicada por un profesional (HU028, ADR-041); al retirarse se
    conserva. No es un plan asignado: no tiene deportista ni fechas.

    `professional_id` es un identificador de Usuarios, sin clave foránea entre bases.
    """

    __tablename__ = "training_plan_template"
    __table_args__ = (
        CheckConstraint("status IN ('PUBLISHED', 'RETIRED')", name="status"),
        CheckConstraint(
            "sport IN ('running', 'cycling', 'swimming', 'strength', 'other')", name="sport"
        ),
        CheckConstraint(
            "goal IN ('HEALTH', 'PERFORMANCE', 'WEIGHT_MANAGEMENT', 'RECREATION', 'COMPETITION')",
            name="goal",
        ),
        CheckConstraint("level IN ('BEGINNER', 'INTERMEDIATE', 'ADVANCED')", name="level"),
        CheckConstraint("duration_weeks BETWEEN 1 AND 52", name="duration_weeks"),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint("(status = 'RETIRED') = (retired_at IS NOT NULL)", name="retired_at"),
    )

    template_id: Mapped[UUID] = mapped_column(primary_key=True)
    professional_id: Mapped[UUID] = mapped_column(index=True)
    title: Mapped[str] = mapped_column(String(120))
    objectives: Mapped[str] = mapped_column(String(2000))
    sport: Mapped[str] = mapped_column(String(16))
    goal: Mapped[str] = mapped_column(String(32))
    level: Mapped[str] = mapped_column(String(16))
    duration_weeks: Mapped[int] = mapped_column(SmallInteger)
    # De 1 a 14 sesiones `{day_of_week, description, duration_minutes}` en el orden del
    # profesional.
    weekly_sessions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
