"""HU009 (ADR-035): sección `nutrition_habits` en `athlete_profile_view`.

Solo expansión: dos columnas nulas (`nutrition_habits` JSONB y `nutrition_habits_version`)
con la misma restricción que las demás secciones (documento y versión juntos, versión desde
1). Sin relleno: el proyector se despliega antes que el productor y la vista se reconstruye
desde los eventos. La reversión elimina las columnas.

Revision ID: 0005_nutrition_habits_view
Revises: 0004_user_directory_full_name
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_nutrition_habits_view"
down_revision: str | None = "0004_user_directory_full_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "athlete_profile_view"
CONSTRAINT = "ck_athlete_profile_view_nutrition_habits_version"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column("nutrition_habits", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(TABLE, sa.Column("nutrition_habits_version", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        op.f(CONSTRAINT),
        TABLE,
        "(nutrition_habits IS NULL) = (nutrition_habits_version IS NULL)"
        " AND (nutrition_habits_version IS NULL OR nutrition_habits_version >= 1)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f(CONSTRAINT), TABLE, type_="check")
    op.drop_column(TABLE, "nutrition_habits_version")
    op.drop_column(TABLE, "nutrition_habits")
