"""HU030 (ADR-041): rutinas alimenticias sugeribles.

Solo expansión (tabla nueva): compatible con la versión anterior del código. La idempotencia
de la publicación reutiliza `command_idempotency` (operación
`publish_nutrition_routine_template`) y los eventos la outbox existente. El catálogo de
alimentos es el de `0006_nutrition_plans` (sin cambios). La reversión elimina la tabla y solo es
segura antes de tener datos reales.

Revision ID: 0014_nutrition_routine_templates
Revises: 0013_health_reports
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_nutrition_routine_templates"
down_revision: str | None = "0013_health_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "nutrition_routine_template"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("professional_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=False),
        sa.Column("goal", sa.String(length=32), nullable=False),
        sa.Column("sport", sa.String(length=16), nullable=True),
        sa.Column("dietary_pattern", sa.String(length=16), nullable=False),
        sa.Column("contains_allergens", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("duration_days", sa.SmallInteger(), nullable=False),
        sa.Column("daily_meals", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PUBLISHED', 'RETIRED')", name=op.f("ck_nutrition_routine_template_status")
        ),
        sa.CheckConstraint(
            "goal IN ('HEALTH', 'PERFORMANCE', 'WEIGHT_MANAGEMENT', 'RECREATION', 'COMPETITION')",
            name=op.f("ck_nutrition_routine_template_goal"),
        ),
        sa.CheckConstraint(
            "sport IS NULL OR sport IN ('running', 'cycling', 'swimming', 'strength', 'other')",
            name=op.f("ck_nutrition_routine_template_sport"),
        ),
        sa.CheckConstraint(
            "dietary_pattern IN ('OMNIVORE', 'VEGETARIAN', 'VEGAN', 'PESCATARIAN', 'FLEXITARIAN')",
            name=op.f("ck_nutrition_routine_template_dietary_pattern"),
        ),
        sa.CheckConstraint(
            "duration_days BETWEEN 1 AND 90",
            name=op.f("ck_nutrition_routine_template_duration_days"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_nutrition_routine_template_version")),
        sa.CheckConstraint(
            "(status = 'RETIRED') = (retired_at IS NOT NULL)",
            name=op.f("ck_nutrition_routine_template_retired_at"),
        ),
        sa.PrimaryKeyConstraint("template_id", name=op.f("pk_nutrition_routine_template")),
    )
    op.create_index(
        op.f("ix_nutrition_routine_template_professional_id"),
        TABLE,
        ["professional_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_nutrition_routine_template_professional_id"), table_name=TABLE)
    op.drop_table(TABLE)
