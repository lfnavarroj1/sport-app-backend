"""HU011: autorizaciones proyectadas e historial de sesiones terminadas.

Tablas reconstruibles a partir de `sportapp.entrenamientos.v1`. Solo expansión:
compatible con la versión anterior. La reversión elimina las tablas; se
reconstruyen reproyectando los eventos.

Revision ID: 0003_training_history
Revises: 0002_training_plan_view
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_training_history"
down_revision: str | None = "0002_training_plan_view"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "training_authorization_view",
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("professional_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "athlete_id", "professional_id", name=op.f("pk_training_authorization_view")
        ),
    )
    op.create_table(
        "training_session_history",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=True),
        sa.Column("sport", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_training_session_history")),
    )
    op.create_index(
        "ix_training_session_history_athlete_order",
        "training_session_history",
        ["athlete_id", "ended_at", "session_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_training_session_history_athlete_order", table_name="training_session_history"
    )
    op.drop_table("training_session_history")
    op.drop_table("training_authorization_view")
