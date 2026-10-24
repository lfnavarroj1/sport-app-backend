"""HU028 (ADR-041): plantillas de plan de entrenamiento sugeribles.

Solo expansión (tabla nueva): compatible con la versión anterior del código. La idempotencia
de la publicación reutiliza `command_idempotency` (operación `publish_training_plan_template`)
y los eventos la outbox existente. La reversión elimina la tabla y solo es segura antes de tener
datos reales.

Revision ID: 0008_training_plan_templates
Revises: 0007_routes
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_training_plan_templates"
down_revision: str | None = "0007_routes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "training_plan_template"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("professional_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("objectives", sa.String(length=2000), nullable=False),
        sa.Column("sport", sa.String(length=16), nullable=False),
        sa.Column("goal", sa.String(length=32), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("duration_weeks", sa.SmallInteger(), nullable=False),
        sa.Column("weekly_sessions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PUBLISHED', 'RETIRED')", name=op.f("ck_training_plan_template_status")
        ),
        sa.CheckConstraint("sport IN ('running', 'cycling', 'swimming', 'strength', 'other')", name=op.f("ck_training_plan_template_sport")),
        sa.CheckConstraint(
            "goal IN ('HEALTH', 'PERFORMANCE', 'WEIGHT_MANAGEMENT', 'RECREATION', 'COMPETITION')",
            name=op.f("ck_training_plan_template_goal"),
        ),
        sa.CheckConstraint("level IN ('BEGINNER', 'INTERMEDIATE', 'ADVANCED')", name=op.f("ck_training_plan_template_level")),
        sa.CheckConstraint(
            "duration_weeks BETWEEN 1 AND 52", name=op.f("ck_training_plan_template_duration_weeks")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_training_plan_template_version")),
        sa.CheckConstraint(
            "(status = 'RETIRED') = (retired_at IS NOT NULL)",
            name=op.f("ck_training_plan_template_retired_at"),
        ),
        sa.PrimaryKeyConstraint("template_id", name=op.f("pk_training_plan_template")),
    )
    op.create_index(
        op.f("ix_training_plan_template_professional_id"), TABLE, ["professional_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_training_plan_template_professional_id"), table_name=TABLE)
    op.drop_table(TABLE)
