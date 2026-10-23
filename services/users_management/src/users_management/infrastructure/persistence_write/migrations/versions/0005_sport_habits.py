"""HU006 (ADR-034): sección de hábitos deportivos del perfil del deportista.

Solo expansión (tabla nueva): compatible con la versión anterior del código. Una fila
vigente por `user_id` (PK y FK local a `user_account`), versión propia, `sports` JSONB de
uno a cinco elementos, `availability` JSONB y `primary_goal` opcional del catálogo. Las
reglas finas de cada deporte las aplica el dominio; las restricciones protegen la forma.
La reversión elimina la tabla: borra los hábitos registrados.

Revision ID: 0005_sport_habits
Revises: 0004_athlete_profile
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_sport_habits"
down_revision: str | None = "0004_athlete_profile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sport_habits",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("sports", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("availability", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("primary_goal", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name=op.f("ck_sport_habits_version")),
        sa.CheckConstraint(
            "jsonb_typeof(sports) = 'array' AND jsonb_array_length(sports) BETWEEN 1 AND 5",
            name=op.f("ck_sport_habits_sports"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(availability) = 'object'", name=op.f("ck_sport_habits_availability")
        ),
        sa.CheckConstraint(
            "primary_goal IS NULL OR primary_goal IN ('HEALTH', 'PERFORMANCE', "
            "'WEIGHT_MANAGEMENT', 'RECREATION', 'COMPETITION')",
            name=op.f("ck_sport_habits_primary_goal"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.user_id"],
            name=op.f("fk_sport_habits_user_id_user_account"),
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_sport_habits")),
    )


def downgrade() -> None:
    op.drop_table("sport_habits")
