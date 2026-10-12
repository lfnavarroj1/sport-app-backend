"""HU010: vista de lectura del plan asignado y deduplicación del proyector.

Tablas reconstruibles a partir de `sportapp.entrenamientos.v1`. Solo expansión:
compatible con la versión anterior. La reversión elimina las tablas; se
reconstruyen reproyectando los eventos.

Revision ID: 0002_training_plan_view
Revises: 0001_baseline
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_training_plan_view"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "training_plan_view",
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("professional_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("objectives", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("sessions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("plan_id", name=op.f("pk_training_plan_view")),
    )
    op.create_index(
        op.f("ix_training_plan_view_athlete_id"),
        "training_plan_view",
        ["athlete_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_training_plan_view_professional_id"),
        "training_plan_view",
        ["professional_id"],
        unique=False,
    )
    op.create_table(
        "processed_event",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_processed_event")),
    )


def downgrade() -> None:
    op.drop_table("processed_event")
    op.drop_index(op.f("ix_training_plan_view_professional_id"), table_name="training_plan_view")
    op.drop_index(op.f("ix_training_plan_view_athlete_id"), table_name="training_plan_view")
    op.drop_table("training_plan_view")
