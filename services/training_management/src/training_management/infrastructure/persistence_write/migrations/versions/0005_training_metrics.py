"""HU034: cálculos inmutables de indicadores y sus entradas normalizadas (ADR-030).

Solo expansión (tablas nuevas): la idempotencia reutiliza `command_idempotency` de HU032
con la operación `calculate_training_metric` y la outbox existente. Entradas y resultado
son `NUMERIC(14, 4)`; la escala del resultado queda en `result_scale`. La reversión
elimina las tablas y solo es segura antes de tener datos reales.

Revision ID: 0005_training_metrics
Revises: 0004_training_sessions
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_training_metrics"
down_revision: str | None = "0004_training_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "training_metric_calculation",
        sa.Column("calculation_id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("metric_code", sa.String(length=64), nullable=False),
        sa.Column("method_code", sa.String(length=64), nullable=False),
        sa.Column("method_version", sa.Integer(), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result_value", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("result_unit", sa.String(length=32), nullable=False),
        sa.Column("result_scale", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("disclaimer_key", sa.String(length=64), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("data_fingerprint", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "status IN ('CONFIRMED')", name=op.f("ck_training_metric_calculation_status")
        ),
        sa.CheckConstraint("version = 1", name=op.f("ck_training_metric_calculation_version")),
        sa.CheckConstraint(
            "method_version >= 1", name=op.f("ck_training_metric_calculation_method_version")
        ),
        sa.CheckConstraint(
            "result_scale BETWEEN 0 AND 4",
            name=op.f("ck_training_metric_calculation_result_scale"),
        ),
        sa.PrimaryKeyConstraint("calculation_id", name=op.f("pk_training_metric_calculation")),
    )
    op.create_index(
        op.f("ix_training_metric_calculation_athlete_id"),
        "training_metric_calculation",
        ["athlete_id"],
        unique=False,
    )
    op.create_table(
        "training_metric_input",
        sa.Column("calculation_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("value", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["calculation_id"],
            ["training_metric_calculation.calculation_id"],
            name="fk_training_metric_input_calculation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("calculation_id", "code", name=op.f("pk_training_metric_input")),
    )


def downgrade() -> None:
    op.drop_table("training_metric_input")
    op.drop_index(
        op.f("ix_training_metric_calculation_athlete_id"),
        table_name="training_metric_calculation",
    )
    op.drop_table("training_metric_calculation")
