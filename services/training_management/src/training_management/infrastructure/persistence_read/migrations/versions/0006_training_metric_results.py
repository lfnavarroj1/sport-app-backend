"""HU034: proyección `training_metric_result` del historial de indicadores (ADR-030).

Expansión de la base de lectura sin backfill: los resultados llegan por
`indicador.calculado.v1` y el consumidor se despliega antes que el productor. Índice
`(athlete_id, measured_at DESC, calculation_id DESC)` para el orden y el cursor keyset.
La reversión elimina la tabla; la base de lectura es reconstruible desde los eventos.

Revision ID: 0006_training_metric_results
Revises: 0005_training_plan_session_view
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_training_metric_results"
down_revision: str | None = "0005_training_plan_session_view"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "training_metric_result",
        sa.Column("calculation_id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("metric_code", sa.String(length=64), nullable=False),
        sa.Column("method_code", sa.String(length=64), nullable=False),
        sa.Column("method_version", sa.Integer(), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result_value", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("result_unit", sa.String(length=32), nullable=False),
        sa.Column("result_scale", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("disclaimer_key", sa.String(length=64), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("calculation_id", name=op.f("pk_training_metric_result")),
    )
    op.create_index(
        "ix_training_metric_result_athlete_order",
        "training_metric_result",
        ["athlete_id", sa.text("measured_at DESC"), sa.text("calculation_id DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_training_metric_result_athlete_order", table_name="training_metric_result")
    op.drop_table("training_metric_result")
