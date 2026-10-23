"""HU014: proyección `nutrition_plan_view` de los planes de alimentación (ADR-037).

Expansión de la base de lectura sin backfill: los planes llegan por
`plan.alimentacion_creado.v1` y el proyector se despliega antes o junto con el productor.
Índice `(athlete_id, created_at DESC, plan_id DESC)` para el orden y el cursor keyset de
`GET /v1/me/nutrition-plans`. La reversión elimina la tabla; la base de lectura es
reconstruible desde los eventos.

Revision ID: 0008_nutrition_plan_view
Revises: 0007_authorized_athletes_index
Create Date: 2026-10-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_nutrition_plan_view"
down_revision: str | None = "0007_authorized_athletes_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_nutrition_plan_view_athlete_order"


def upgrade() -> None:
    op.create_table(
        "nutrition_plan_view",
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("professional_id", sa.Uuid(), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("plan_id", name=op.f("pk_nutrition_plan_view")),
    )
    op.create_index(
        INDEX,
        "nutrition_plan_view",
        ["athlete_id", sa.text("created_at DESC"), sa.text("plan_id DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="nutrition_plan_view")
    op.drop_table("nutrition_plan_view")
