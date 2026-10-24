"""HU028: proyección `training_plan_template_view` de las plantillas de plan (ADR-041).

Expansión de la base de lectura sin backfill: las plantillas llegan por
`plantilla_entrenamiento.publicada.v1` / `.retirada.v1`, y el proyector se despliega antes o
junto con el productor. Índices para las sugerencias por estado y deporte, para
`GET /v1/me/training-plan-templates` y para la lista no personalizada. La reversión elimina la
tabla; la base de lectura es reconstruible.

Revision ID: 0010_training_plan_template_view
Revises: 0009_route_view
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_training_plan_template_view"
down_revision: str | None = "0009_route_view"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "training_plan_template_view"
SUGGESTION_INDEX = "ix_training_plan_template_view_suggestion"
PROFESSIONAL_INDEX = "ix_training_plan_template_view_professional_order"
LATEST_INDEX = "ix_training_plan_template_view_status_published"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("professional_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("sport", sa.String(length=16), nullable=False),
        sa.Column("goal", sa.String(length=32), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("template_id", name=op.f("pk_training_plan_template_view")),
    )
    op.create_index(
        SUGGESTION_INDEX,
        TABLE,
        ["status", "sport", sa.text("published_at DESC"), sa.text("template_id DESC")],
        unique=False,
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
    op.drop_index(SUGGESTION_INDEX, table_name=TABLE)
    op.drop_table(TABLE)
