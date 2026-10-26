"""HU030: proyección `nutrition_routine_template_view` de las rutinas alimenticias (ADR-041).

Expansión de la base de lectura sin backfill: las rutinas llegan por
`plantilla_rutina.publicada.v1` / `.retirada.v1`, y el proyector se despliega antes o junto con
el productor. Índices para las sugerencias (estado y orden de publicación) y para
`GET /v1/me/nutrition-routine-templates`. La exclusión por alérgenos y patrón se evalúa sobre las
columnas `contains_allergens` y `dietary_pattern`. La reversión elimina la tabla; la base de
lectura es reconstruible.

Revision ID: 0014_routine_template_view
Revises: 0013_health_report_view
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_routine_template_view"
down_revision: str | None = "0013_health_report_view"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "nutrition_routine_template_view"
PROFESSIONAL_INDEX = "ix_nutrition_routine_template_view_professional_order"
LATEST_INDEX = "ix_nutrition_routine_template_view_status_published"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("professional_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("goal", sa.String(length=32), nullable=False),
        sa.Column("sport", sa.String(length=16), nullable=True),
        sa.Column("dietary_pattern", sa.String(length=16), nullable=False),
        sa.Column("contains_allergens", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("template_id", name=op.f("pk_nutrition_routine_template_view")),
    )
    op.create_index(
        PROFESSIONAL_INDEX,
        TABLE,
        ["professional_id", sa.text("created_at DESC"), sa.text("template_id DESC")],
        unique=False,
    )
    op.create_index(
        LATEST_INDEX,
        TABLE,
        ["status", sa.text("published_at DESC"), sa.text("template_id DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(LATEST_INDEX, table_name=TABLE)
    op.drop_index(PROFESSIONAL_INDEX, table_name=TABLE)
    op.drop_table(TABLE)
