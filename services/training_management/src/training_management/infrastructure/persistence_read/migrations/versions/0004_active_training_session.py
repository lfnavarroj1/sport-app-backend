"""HU032: vista `active_training_session` (ADR-029); el historial de `0003` se reutiliza.

Solo expansión; la base de lectura es reconstruible desde los eventos.

Revision ID: 0004_active_training_session
Revises: 0003_training_history
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_active_training_session"
down_revision: str | None = "0003_training_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "active_training_session",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=True),
        sa.Column("sport", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_active_training_session")),
    )
    op.create_index(
        op.f("ix_active_training_session_athlete_id"),
        "active_training_session",
        ["athlete_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_active_training_session_athlete_id"), table_name="active_training_session"
    )
    op.drop_table("active_training_session")
