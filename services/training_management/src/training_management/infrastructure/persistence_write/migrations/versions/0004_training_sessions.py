"""HU032: sesiones de entrenamiento e idempotencia por deportista y operación (ADR-029).

Solo expansión (tablas nuevas): `idempotency_key` de HU010 no cambia; los comandos de
HU032 usan `command_idempotency` con clave (`actor_id`, `operation`, `key`) y retención
de 30 días. La reversión elimina las tablas y solo es segura antes de tener datos reales.

Revision ID: 0004_training_sessions
Revises: 0003_authorization_backfill
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_training_sessions"
down_revision: str | None = "0003_authorization_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "training_session",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=True),
        sa.Column("sport", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'FINISHED')", name=op.f("ck_training_session_status")
        ),
        sa.CheckConstraint(
            "sport IN ('running', 'cycling', 'swimming', 'strength', 'other')",
            name=op.f("ck_training_session_sport"),
        ),
        sa.CheckConstraint(
            "(status = 'ACTIVE' AND ended_at IS NULL AND duration_seconds IS NULL AND version = 1)"
            " OR (status = 'FINISHED' AND ended_at >= started_at AND duration_seconds >= 0"
            " AND version = 2)",
            name=op.f("ck_training_session_transition"),
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["training_plan.plan_id"],
            name=op.f("fk_training_session_plan_id_training_plan"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_training_session")),
    )
    op.create_index(
        op.f("ix_training_session_athlete_id"), "training_session", ["athlete_id"], unique=False
    )
    op.create_index(
        "uq_training_session_athlete_active",
        "training_session",
        ["athlete_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_table(
        "command_idempotency",
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("key", sa.Uuid(), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "actor_id", "operation", "key", name=op.f("pk_command_idempotency")
        ),
    )
    op.create_index(
        op.f("ix_command_idempotency_expires_at"),
        "command_idempotency",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_command_idempotency_expires_at"), table_name="command_idempotency")
    op.drop_table("command_idempotency")
    op.drop_index(
        "uq_training_session_athlete_active",
        table_name="training_session",
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.drop_index(op.f("ix_training_session_athlete_id"), table_name="training_session")
    op.drop_table("training_session")
