"""HU042: proyección `health_report_view` de los reportes de salud (ADR-037).

Expansión de la base de lectura sin backfill: los reportes llegan por
`reporte_salud.registrado.v1` y el proyector se despliega antes que el productor. Índice
`(athlete_id, occurred_at DESC, report_id DESC)` para el orden y el cursor keyset de HU012.
La reversión elimina la tabla; la base de lectura es reconstruible desde los eventos.

Revision ID: 0013_health_report_view
Revises: 0012_food_intake_view
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_health_report_view"
down_revision: str | None = "0012_food_intake_view"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "health_report_view"
ORDER_INDEX = "ix_health_report_view_athlete_order"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("body_area", sa.String(length=16), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_recovery_days", sa.SmallInteger(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("report_id", name=op.f("pk_health_report_view")),
    )
    op.create_index(
        ORDER_INDEX,
        TABLE,
        ["athlete_id", sa.text("occurred_at DESC"), sa.text("report_id DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(ORDER_INDEX, table_name=TABLE)
    op.drop_table(TABLE)
