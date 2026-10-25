"""HU043: proyección `food_intake_view` de los consumos de alimentos (ADR-037).

Expansión de la base de lectura sin backfill: los consumos llegan por
`consumo_alimento.registrado.v1` y el proyector se despliega antes o junto con el productor.
Ítems en JSONB e índice `(athlete_id, consumed_at DESC, intake_id DESC)` para el historial
keyset de HU013. La reversión elimina la tabla; la base de lectura es reconstruible desde
los eventos.

Revision ID: 0012_food_intake_view
Revises: 0011_session_monitoring_views
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_food_intake_view"
down_revision: str | None = "0011_session_monitoring_views"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_food_intake_view_athlete_order"


def upgrade() -> None:
    op.create_table(
        "food_intake_view",
        sa.Column("intake_id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("meal_type", sa.String(length=16), nullable=False),
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("catalog_version", sa.String(length=64), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("intake_id", name=op.f("pk_food_intake_view")),
    )
    op.create_index(
        INDEX,
        "food_intake_view",
        ["athlete_id", sa.text("consumed_at DESC"), sa.text("intake_id DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="food_intake_view")
    op.drop_table("food_intake_view")
