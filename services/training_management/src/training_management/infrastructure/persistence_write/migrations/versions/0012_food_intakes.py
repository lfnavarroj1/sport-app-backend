"""HU043 (ADR-037): consumos de alimentos y bebidas del deportista.

Solo expansión, compatible con la versión anterior del código:
- `food_intake`: consumo inmutable en la versión 1 (`meal_type`, `consumed_at`, versión del
  catálogo y huella de los datos para la idempotencia por `intake_id`);
- `food_intake_item`: alimentos del consumo en el orden enviado, con clave foránea local al
  catálogo `food_catalog_item` que precargó `0006_nutrition_plans` (no se crea otra versión).
La idempotencia reutiliza `command_idempotency` (`register_food_intake`, 30 días).

La reversión elimina las tablas; solo es segura antes de tener datos reales (pierde los
consumos registrados).

Revision ID: 0012_food_intakes
Revises: 0011_session_reminders
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_food_intakes"
down_revision: str | None = "0011_session_reminders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "food_intake",
        sa.Column("intake_id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("meal_type", sa.String(length=16), nullable=False),
        sa.Column("catalog_version", sa.String(length=64), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("data_fingerprint", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "meal_type IN ('BREAKFAST', 'LUNCH', 'DINNER', 'SNACK', 'HYDRATION')",
            name=op.f("ck_food_intake_meal_type"),
        ),
        sa.CheckConstraint("version = 1", name=op.f("ck_food_intake_version")),
        sa.PrimaryKeyConstraint("intake_id", name=op.f("pk_food_intake")),
    )
    op.create_index(op.f("ix_food_intake_athlete_id"), "food_intake", ["athlete_id"], unique=False)
    op.create_table(
        "food_intake_item",
        sa.Column("intake_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("food_code", sa.String(length=64), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=5, scale=1), nullable=False),
        sa.Column("unit", sa.String(length=8), nullable=False),
        sa.CheckConstraint(
            "quantity BETWEEN 0.1 AND 5000.0", name=op.f("ck_food_intake_item_quantity")
        ),
        sa.CheckConstraint(
            "unit IN ('G', 'ML', 'UNIT', 'CUP', 'TBSP')", name=op.f("ck_food_intake_item_unit")
        ),
        sa.ForeignKeyConstraint(
            ["food_code"],
            ["food_catalog_item.food_code"],
            name=op.f("fk_food_intake_item_food_code_food_catalog_item"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["intake_id"],
            ["food_intake.intake_id"],
            name=op.f("fk_food_intake_item_intake_id_food_intake"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("intake_id", "position", name=op.f("pk_food_intake_item")),
    )


def downgrade() -> None:
    op.drop_table("food_intake_item")
    op.drop_index(op.f("ix_food_intake_athlete_id"), table_name="food_intake")
    op.drop_table("food_intake")
