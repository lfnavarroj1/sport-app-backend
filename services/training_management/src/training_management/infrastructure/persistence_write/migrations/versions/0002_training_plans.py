"""HU010: autorización del deportista, plan de entrenamiento, outbox e idempotencia.

Solo expansión (tablas nuevas): compatible con la versión anterior del código.
La reversión elimina las tablas y solo es segura antes de tener datos reales.

Revision ID: 0002_training_plans
Revises: 0001_baseline
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_training_plans"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "training_authorization",
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("professional_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED')", name=op.f("ck_training_authorization_status")
        ),
        sa.PrimaryKeyConstraint(
            "athlete_id", "professional_id", name=op.f("pk_training_authorization")
        ),
    )
    op.create_index(
        "ix_training_authorization_professional_id",
        "training_authorization",
        ["professional_id"],
        unique=False,
    )
    op.create_table(
        "training_plan",
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("professional_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("objectives", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("status IN ('ASSIGNED')", name=op.f("ck_training_plan_status")),
        sa.CheckConstraint("start_date <= end_date", name=op.f("ck_training_plan_period")),
        sa.PrimaryKeyConstraint("plan_id", name=op.f("pk_training_plan")),
    )
    op.create_index(
        op.f("ix_training_plan_athlete_id"), "training_plan", ["athlete_id"], unique=False
    )
    op.create_index(
        op.f("ix_training_plan_professional_id"),
        "training_plan",
        ["professional_id"],
        unique=False,
    )
    op.create_table(
        "training_plan_session",
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("scheduled_date", sa.Date(), nullable=False),
        sa.Column("sport", sa.String(length=16), nullable=False),
        sa.Column("duration_minutes", sa.SmallInteger(), nullable=False),
        sa.Column("intensity_rpe", sa.SmallInteger(), nullable=False),
        sa.CheckConstraint(
            "sport IN ('running', 'cycling', 'swimming', 'strength', 'other')",
            name=op.f("ck_training_plan_session_sport"),
        ),
        sa.CheckConstraint(
            "duration_minutes BETWEEN 1 AND 600",
            name=op.f("ck_training_plan_session_duration_minutes"),
        ),
        sa.CheckConstraint(
            "intensity_rpe BETWEEN 1 AND 10", name=op.f("ck_training_plan_session_intensity_rpe")
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["training_plan.plan_id"],
            name=op.f("fk_training_plan_session_plan_id_training_plan"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("plan_id", "position", name=op.f("pk_training_plan_session")),
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
    op.drop_table("training_plan_session")
    op.drop_index(op.f("ix_training_plan_professional_id"), table_name="training_plan")
    op.drop_index(op.f("ix_training_plan_athlete_id"), table_name="training_plan")
    op.drop_table("training_plan")
    op.drop_index("ix_training_authorization_professional_id", table_name="training_authorization")
    op.drop_table("training_authorization")
