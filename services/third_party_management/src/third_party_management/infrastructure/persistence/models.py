"""Modelos SQLAlchemy de la base única `terceros` (HU031, ADR-042 §2).

Nunca se exponen como respuestas públicas. `user_id` es un identificador de Usuarios: no hay
claves foráneas hacia otra base. Los tokens solo se guardan cifrados (AES-256-GCM).
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sportapp_platform.database import new_metadata

IDEMPOTENCY_PRIMARY_KEY = "pk_idempotency_key"
# Una sola corrida `QUEUED`/`RUNNING` por deportista (`409 sync_in_progress`).
ACTIVE_SYNC_RUN_INDEX = "uq_sync_run_active"
# Una cuenta de Strava solo puede estar `ACTIVE` para un deportista (R).
ACTIVE_ATHLETE_INDEX = "uq_external_account_athlete_active"
SYNC_ERROR_CODES = (
    "rate_limited",
    "provider_unavailable",
    "circuit_open",
    "token_expired",
    "insufficient_scope",
    "provider_rejected",
    "invalid_response",
    "invalid_activity",
    "integration_revoked",
)


class Base(DeclarativeBase):
    metadata = new_metadata()


class ExternalAccountRow(Base):
    """Vinculación del deportista con Strava; `ACTIVE` ⇔ tokens presentes."""

    __tablename__ = "external_account"
    __table_args__ = (
        CheckConstraint("provider = 'STRAVA'", name="provider"),
        CheckConstraint("status IN ('ACTIVE', 'REVOKED', 'EXPIRED')", name="status"),
        CheckConstraint(
            "(status = 'ACTIVE' AND access_token_enc IS NOT NULL "
            "AND refresh_token_enc IS NOT NULL AND token_expires_at IS NOT NULL) "
            "OR (status <> 'ACTIVE' AND access_token_enc IS NULL AND refresh_token_enc IS NULL)",
            name="tokens",
        ),
        Index(
            ACTIVE_ATHLETE_INDEX,
            "provider",
            "external_athlete_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(16))
    external_athlete_id: Mapped[str] = mapped_column(String(32))
    access_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    refresh_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(64)))
    consent_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(BigInteger)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OAuthStateRow(Base):
    """`state` antifalsificación de un solo uso; vence a los 10 minutos."""

    __tablename__ = "oauth_state"

    state: Mapped[str] = mapped_column(String(512), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SyncRunRow(Base):
    """Corrida de sincronización manual. `correlation_id` del comando (R) acompaña los
    eventos que produce."""

    __tablename__ = "sync_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'RATE_LIMITED')",
            name="status",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            + ", ".join(f"'{code}'" for code in SYNC_ERROR_CODES)
            + ")",
            name="error_code",
        ),
        CheckConstraint("imported_count >= 0", name="imported_count"),
        Index(
            ACTIVE_SYNC_RUN_INDEX,
            "user_id",
            unique=True,
            postgresql_where=text("status IN ('QUEUED', 'RUNNING')"),
        ),
        Index("ix_sync_run_user_id_requested_at", "user_id", "requested_at"),
        Index(
            "ix_sync_run_pending",
            "requested_at",
            postgresql_where=text("status IN ('QUEUED', 'RUNNING')"),
        ),
    )

    sync_run_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID]
    status: Mapped[str] = mapped_column(String(16))
    imported_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error_code: Mapped[str | None] = mapped_column(String(32))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    correlation_id: Mapped[UUID | None]


class ImportedActivityRow(Base):
    """Deduplicación por `(provider, external_activity_id)` y hash (CA5). `started_at` (R)
    fija el `after` de la siguiente corrida."""

    __tablename__ = "imported_activity"
    __table_args__ = (
        CheckConstraint("provider = 'STRAVA'", name="provider"),
        CheckConstraint("version >= 1", name="version"),
        Index("ix_imported_activity_user_id_started_at", "user_id", "started_at"),
    )

    provider: Mapped[str] = mapped_column(String(16), primary_key=True)
    external_activity_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[UUID]
    activity_id: Mapped[UUID] = mapped_column(unique=True)
    external_version: Mapped[str] = mapped_column(String(128))
    payload_hash: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(BigInteger)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IdempotencyKeyRow(Base):
    """Resultado de un comando reintentable, vigente 24 horas desde `created_at`."""

    __tablename__ = "idempotency_key"

    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    operation: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[UUID] = mapped_column(primary_key=True)
    result_id: Mapped[UUID]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class OutboxEventRow(Base):
    """Outbox transaccional (CMP-002) de `actividad.importada.v1`; mismas columnas que la
    outbox de Servicios complementarios. Un pendiente nunca se elimina; un publicado se
    conserva siete días."""

    __tablename__ = "outbox_event"
    __table_args__ = (
        Index(
            "idx_outbox_pending",
            "next_attempt_at",
            "occurred_at",
            postgresql_where=text("published_at IS NULL"),
        ),
        Index(
            "idx_outbox_published",
            "published_at",
            postgresql_where=text("published_at IS NOT NULL"),
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
