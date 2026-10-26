"""HU031: vinculación con Strava, sincronización manual e importación (ADR-042 §2).

Primera revisión del historial de la base única `terceros`:

- `external_account`: vinculación con tokens cifrados (`ACTIVE` ⇔ tokens presentes) y una
  sola cuenta de Strava `ACTIVE` por atleta externo (R).
- `oauth_state`: `state` antifalsificación de un solo uso.
- `sync_run`: corridas con índice único parcial (una `QUEUED`/`RUNNING` por deportista) y
  el índice de pendientes que reclama el worker.
- `imported_activity`: deduplicación por `(provider, external_activity_id)` y hash.
- `idempotency_key` y `outbox_event` (mismas columnas que en Servicios complementarios).

La reversión borra todas las tablas: solo es segura antes de tener vinculaciones reales.

Revision ID: 0001_strava_connections
Revises:
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_strava_connections"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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


def upgrade() -> None:
    op.create_table(
        "external_account",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("external_athlete_id", sa.String(length=32), nullable=False),
        sa.Column("access_token_enc", sa.LargeBinary(), nullable=True),
        sa.Column("refresh_token_enc", sa.LargeBinary(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", postgresql.ARRAY(sa.String(length=64)), nullable=False),
        sa.Column("consent_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider = 'STRAVA'", name=op.f("ck_external_account_provider")),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED', 'EXPIRED')", name=op.f("ck_external_account_status")
        ),
        sa.CheckConstraint(
            "(status = 'ACTIVE' AND access_token_enc IS NOT NULL "
            "AND refresh_token_enc IS NOT NULL AND token_expires_at IS NOT NULL) "
            "OR (status <> 'ACTIVE' AND access_token_enc IS NULL AND refresh_token_enc IS NULL)",
            name=op.f("ck_external_account_tokens"),
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_external_account")),
    )
    op.create_index(
        "uq_external_account_athlete_active",
        "external_account",
        ["provider", "external_athlete_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_table(
        "oauth_state",
        sa.Column("state", sa.String(length=512), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("state", name=op.f("pk_oauth_state")),
    )
    op.create_index(op.f("ix_oauth_state_user_id"), "oauth_state", ["user_id"], unique=False)
    op.create_table(
        "sync_run",
        sa.Column("sync_run_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("imported_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_code", sa.String(length=32), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("correlation_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'RATE_LIMITED')",
            name=op.f("ck_sync_run_status"),
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            + ", ".join(f"'{code}'" for code in SYNC_ERROR_CODES)
            + ")",
            name=op.f("ck_sync_run_error_code"),
        ),
        sa.CheckConstraint("imported_count >= 0", name=op.f("ck_sync_run_imported_count")),
        sa.PrimaryKeyConstraint("sync_run_id", name=op.f("pk_sync_run")),
    )
    op.create_index(
        "uq_sync_run_active",
        "sync_run",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING')"),
    )
    op.create_index(
        "ix_sync_run_user_id_requested_at", "sync_run", ["user_id", "requested_at"], unique=False
    )
    op.create_index(
        "ix_sync_run_pending",
        "sync_run",
        ["requested_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING')"),
    )
    op.create_table(
        "imported_activity",
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("external_activity_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("activity_id", sa.Uuid(), nullable=False),
        sa.Column("external_version", sa.String(length=128), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider = 'STRAVA'", name=op.f("ck_imported_activity_provider")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_imported_activity_version")),
        sa.PrimaryKeyConstraint(
            "provider", "external_activity_id", name=op.f("pk_imported_activity")
        ),
        sa.UniqueConstraint("activity_id", name=op.f("uq_imported_activity_activity_id")),
    )
    op.create_index(
        "ix_imported_activity_user_id_started_at",
        "imported_activity",
        ["user_id", "started_at"],
        unique=False,
    )
    op.create_table(
        "idempotency_key",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("key", sa.Uuid(), nullable=False),
        sa.Column("result_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "operation", "key", name=op.f("pk_idempotency_key")),
    )
    op.create_index(
        op.f("ix_idempotency_key_created_at"), "idempotency_key", ["created_at"], unique=False
    )
    op.create_table(
        "outbox_event",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_version", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=True),
        sa.Column("causation_id", sa.String(length=128), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(length=256), nullable=True),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_outbox_event")),
    )
    op.create_index(
        "idx_outbox_pending",
        "outbox_event",
        ["next_attempt_at", "occurred_at"],
        unique=False,
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_index(
        "idx_outbox_published",
        "outbox_event",
        ["published_at"],
        unique=False,
        postgresql_where=sa.text("published_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("outbox_event")
    op.drop_table("idempotency_key")
    op.drop_table("imported_activity")
    op.drop_table("sync_run")
    op.drop_table("oauth_state")
    op.drop_table("external_account")
