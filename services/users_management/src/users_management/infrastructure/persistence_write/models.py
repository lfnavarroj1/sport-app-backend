"""Modelos SQLAlchemy de la base de escritura (fuente de verdad y `outbox_event`).

Nunca se exponen como respuestas públicas.
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
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
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
        CheckConstraint("status IN ('active', 'disabled')", name="status"),
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


class RefreshTokenRow(WriteBase):
    """Refresh tokens de HU003. Solo el hash SHA-256; nunca se proyecta al modelo de lectura."""

    __tablename__ = "refresh_token"

    token_id: Mapped[UUID] = mapped_column(primary_key=True)
    family_id: Mapped[UUID] = mapped_column(index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("user_account.user_id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by: Mapped[UUID | None]
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginFailureWindowRow(WriteBase):
    """Intentos fallidos por correo + IP (HU003). La clave es un hash: sin correos en claro."""

    __tablename__ = "login_failure_window"

    throttle_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# HU005 (ADR-034): una fila vigente por deportista y sección, con versión propia.
DEMOGRAPHIC_PRIMARY_KEY = "pk_demographic_profile"
PHYSIOLOGICAL_PRIMARY_KEY = "pk_physiological_profile"
# HU006: tercera sección del perfil.
SPORT_HABITS_PRIMARY_KEY = "pk_sport_habits"
# HU009 (ADR-035): cuarta sección del perfil.
NUTRITION_HABITS_PRIMARY_KEY = "pk_nutrition_habits"


class DemographicProfileRow(WriteBase):
    """Sección demográfica del perfil. Datos personales: `__repr__` sin valores."""

    __tablename__ = "demographic_profile"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint(
            "gender IN ('FEMALE', 'MALE', 'NON_BINARY', 'PREFER_NOT_TO_SAY')", name="gender"
        ),
        CheckConstraint("country_code ~ '^[A-Z]{2}$'", name="country_code"),
        CheckConstraint("char_length(city) BETWEEN 2 AND 80", name="city"),
        CheckConstraint(
            "marital_status IS NULL OR marital_status IN ('SINGLE', 'MARRIED', "
            "'DOMESTIC_PARTNERSHIP', 'DIVORCED', 'WIDOWED', 'PREFER_NOT_TO_SAY')",
            name="marital_status",
        ),
        CheckConstraint(
            "occupation IS NULL OR char_length(occupation) BETWEEN 2 AND 80", name="occupation"
        ),
        CheckConstraint(
            "income_range IS NULL OR income_range IN ('LT_1_SMMLV', '1_2_SMMLV', "
            "'2_4_SMMLV', 'GT_4_SMMLV', 'PREFER_NOT_TO_SAY')",
            name="income_range",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("user_account.user_id"), primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger)
    birth_date: Mapped[date] = mapped_column(Date)
    gender: Mapped[str] = mapped_column(String(32))
    country_code: Mapped[str] = mapped_column(String(2))
    city: Mapped[str] = mapped_column(String(80))
    marital_status: Mapped[str | None] = mapped_column(String(32))
    occupation: Mapped[str | None] = mapped_column(String(80))
    income_range: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"DemographicProfileRow(user_id={self.user_id}, version={self.version})"


class PhysiologicalProfileRow(WriteBase):
    """Sección fisiológica del perfil. Datos de salud: `__repr__` sin valores."""

    __tablename__ = "physiological_profile"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint("weight_kg BETWEEN 20.0 AND 300.0", name="weight_kg"),
        CheckConstraint("height_cm BETWEEN 100 AND 250", name="height_cm"),
        CheckConstraint("bmi > 0", name="bmi"),
        CheckConstraint(
            "resting_heart_rate_bpm IS NULL OR resting_heart_rate_bpm BETWEEN 30 AND 220",
            name="resting_heart_rate",
        ),
        CheckConstraint(
            # Ambas o ninguna; un `NULL` suelto haría pasar la restricción sin evaluarla.
            "(systolic_mmhg IS NULL) = (diastolic_mmhg IS NULL) AND (systolic_mmhg IS NULL "
            "OR (systolic_mmhg BETWEEN 60 AND 250 AND diastolic_mmhg BETWEEN 30 AND 150 "
            "AND systolic_mmhg > diastolic_mmhg))",
            name="blood_pressure",
        ),
        CheckConstraint(
            "body_temperature_c IS NULL OR body_temperature_c BETWEEN 34.0 AND 42.0",
            name="body_temperature",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("user_account.user_id"), primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger)
    health_consent_version: Mapped[str] = mapped_column(String(64))
    health_consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(4, 1))
    height_cm: Mapped[int] = mapped_column(Integer)
    bmi: Mapped[Decimal] = mapped_column(Numeric(4, 1))
    resting_heart_rate_bpm: Mapped[int | None] = mapped_column(Integer)
    systolic_mmhg: Mapped[int | None] = mapped_column(Integer)
    diastolic_mmhg: Mapped[int | None] = mapped_column(Integer)
    body_temperature_c: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"PhysiologicalProfileRow(user_id={self.user_id}, version={self.version})"


class SportHabitsRow(WriteBase):
    """Sección de hábitos deportivos (HU006). Datos personales: `__repr__` sin valores."""

    __tablename__ = "sport_habits"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint(
            "jsonb_typeof(sports) = 'array' AND jsonb_array_length(sports) BETWEEN 1 AND 5",
            name="sports",
        ),
        CheckConstraint("jsonb_typeof(availability) = 'object'", name="availability"),
        CheckConstraint(
            "primary_goal IS NULL OR primary_goal IN ('HEALTH', 'PERFORMANCE', "
            "'WEIGHT_MANAGEMENT', 'RECREATION', 'COMPETITION')",
            name="primary_goal",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("user_account.user_id"), primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger)
    sports: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    availability: Mapped[dict[str, Any]] = mapped_column(JSONB)
    primary_goal: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"SportHabitsRow(user_id={self.user_id}, version={self.version})"


class NutritionHabitsRow(WriteBase):
    """Sección de hábitos alimenticios (HU009). Alergias y restricciones son datos de salud:
    `__repr__` sin valores."""

    __tablename__ = "nutrition_habits"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint(
            "diet_pattern IN ('OMNIVORE', 'VEGETARIAN', 'VEGAN', 'PESCATARIAN', 'FLEXITARIAN')",
            name="diet_pattern",
        ),
        CheckConstraint(
            "jsonb_typeof(restrictions) = 'array' AND jsonb_array_length(restrictions) <= 7",
            name="restrictions",
        ),
        CheckConstraint(
            "jsonb_typeof(allergies) = 'array' AND jsonb_array_length(allergies) <= 9",
            name="allergies",
        ),
        CheckConstraint("meals_per_day BETWEEN 1 AND 8", name="meals_per_day"),
        CheckConstraint(
            "hydration_liters_per_day IS NULL OR hydration_liters_per_day BETWEEN 0.5 AND 10.0",
            name="hydration",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("user_account.user_id"), primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger)
    health_consent_version: Mapped[str] = mapped_column(String(64))
    health_consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    diet_pattern: Mapped[str] = mapped_column(String(16))
    restrictions: Mapped[list[str]] = mapped_column(JSONB)
    allergies: Mapped[list[str]] = mapped_column(JSONB)
    meals_per_day: Mapped[int] = mapped_column(Integer)
    hydration_liters_per_day: Mapped[Decimal | None] = mapped_column(Numeric(3, 1))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"NutritionHabitsRow(user_id={self.user_id}, version={self.version})"
