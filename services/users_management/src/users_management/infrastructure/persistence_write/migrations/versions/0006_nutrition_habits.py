"""HU009 (ADR-035): sección de hábitos alimenticios del perfil del deportista.

Solo expansión (tabla nueva): compatible con la versión anterior del código. Una fila
vigente por `user_id` (PK y FK local a `user_account`), versión propia, consentimiento de
datos de salud, `diet_pattern` del catálogo, `restrictions` y `allergies` JSONB (listas de
catálogos cerrados, posiblemente vacías), `meals_per_day` y `hydration_liters_per_day`
`NUMERIC(3,1)` opcional. El paquete sugería `0005_nutrition_habits`, pero esa posición ya
la ocupa `0005_sport_habits` (HU006). La reversión elimina la tabla: borra los hábitos
alimenticios registrados.

Revision ID: 0006_nutrition_habits
Revises: 0005_sport_habits
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_nutrition_habits"
down_revision: str | None = "0005_sport_habits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "nutrition_habits",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("health_consent_version", sa.String(length=64), nullable=False),
        sa.Column("health_consent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("diet_pattern", sa.String(length=16), nullable=False),
        sa.Column("restrictions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("allergies", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("meals_per_day", sa.Integer(), nullable=False),
        sa.Column("hydration_liters_per_day", sa.Numeric(precision=3, scale=1), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name=op.f("ck_nutrition_habits_version")),
        sa.CheckConstraint(
            "diet_pattern IN ('OMNIVORE', 'VEGETARIAN', 'VEGAN', 'PESCATARIAN', 'FLEXITARIAN')",
            name=op.f("ck_nutrition_habits_diet_pattern"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(restrictions) = 'array' AND jsonb_array_length(restrictions) <= 7",
            name=op.f("ck_nutrition_habits_restrictions"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(allergies) = 'array' AND jsonb_array_length(allergies) <= 9",
            name=op.f("ck_nutrition_habits_allergies"),
        ),
        sa.CheckConstraint(
            "meals_per_day BETWEEN 1 AND 8", name=op.f("ck_nutrition_habits_meals_per_day")
        ),
        sa.CheckConstraint(
            "hydration_liters_per_day IS NULL OR hydration_liters_per_day BETWEEN 0.5 AND 10.0",
            name=op.f("ck_nutrition_habits_hydration"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.user_id"],
            name=op.f("fk_nutrition_habits_user_id_user_account"),
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_nutrition_habits")),
    )


def downgrade() -> None:
    op.drop_table("nutrition_habits")
