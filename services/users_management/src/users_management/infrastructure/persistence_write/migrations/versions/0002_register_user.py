"""HU001: cuenta de usuario, outbox transaccional e idempotencia.

Solo expansión (tablas nuevas): compatible con la versión anterior del código.
La reversión elimina las tablas y solo es segura antes de tener datos reales.

Revision ID: 0002_register_user
Revises: 0001_baseline
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_register_user"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_account",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("full_name", sa.String(length=120), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("accepted_policies_version", sa.String(length=32), nullable=False),
        sa.Column("policies_accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "actor_type IN ('athlete', 'organizer', 'service_provider')",
            name=op.f("ck_user_account_actor_type"),
        ),
        sa.CheckConstraint("status IN ('active')", name=op.f("ck_user_account_status")),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_user_account")),
        sa.UniqueConstraint("email", name=op.f("uq_user_account_email")),
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
    op.create_table(
        "idempotency_key",
        sa.Column("key", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_idempotency_key")),
    )
    op.create_index(
        op.f("ix_idempotency_key_expires_at"), "idempotency_key", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_idempotency_key_expires_at"), table_name="idempotency_key")
    op.drop_table("idempotency_key")
    op.drop_index(
        "idx_outbox_pending",
        table_name="outbox_event",
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.drop_table("outbox_event")
    op.drop_table("user_account")
