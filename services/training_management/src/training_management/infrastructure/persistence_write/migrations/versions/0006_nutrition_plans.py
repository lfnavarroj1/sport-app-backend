"""HU014 (ADR-037): catálogo de alimentos `food-catalog-2026-10-13.v1` y planes de alimentación.

Solo expansión (tablas nuevas): compatible con la versión anterior del código. La
idempotencia reutiliza `idempotency_key` de HU010 (operación `create_nutrition_plan`) y la
outbox existente.

La precarga del catálogo es idempotente (UPSERT por `food_code`): repetirla no duplica filas
y restituye los valores de esta versión. Los datos quedan congelados aquí: una corrección o
ampliación del catálogo es otra migración con otra versión, nunca un cambio de esta. La
lista es la propuesta del 2026-10-22 pendiente de ratificar en la ficha de HU014.

La reversión elimina las tablas y solo es segura antes de tener datos reales.

Revision ID: 0006_nutrition_plans
Revises: 0005_training_metrics
Create Date: 2026-10-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_nutrition_plans"
down_revision: str | None = "0005_training_metrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATALOG_VERSION = "food-catalog-2026-10-13.v1"

_MEAT = ["VEGAN", "VEGETARIAN", "PESCATARIAN"]
_SEAFOOD = ["VEGAN", "VEGETARIAN"]
_DAIRY = ["VEGAN", "LACTOSE_FREE"]

# (food_code, category, default_unit, allowed_units, allergens, incompatible_with)
FOODS: list[tuple[str, str, str, list[str], list[str], list[str]]] = [
    ("OATS", "GRAIN", "G", ["G", "CUP", "TBSP"], [], []),
    ("WHITE_RICE", "GRAIN", "G", ["G", "CUP"], [], []),
    ("BROWN_RICE", "GRAIN", "G", ["G", "CUP"], [], []),
    ("QUINOA", "GRAIN", "G", ["G", "CUP"], [], []),
    ("WHOLE_WHEAT_BREAD", "GRAIN", "G", ["G", "UNIT"], ["WHEAT"], ["GLUTEN_FREE", "KETO"]),
    ("PASTA", "GRAIN", "G", ["G", "CUP"], ["WHEAT"], ["GLUTEN_FREE", "KETO"]),
    ("CORN_AREPA", "GRAIN", "G", ["G", "UNIT"], [], ["KETO"]),
    ("CHICKEN_BREAST", "PROTEIN", "G", ["G"], [], _MEAT),
    ("BEEF", "PROTEIN", "G", ["G"], [], _MEAT),
    ("PORK_LOIN", "PROTEIN", "G", ["G"], [], [*_MEAT, "HALAL", "KOSHER"]),
    ("SALMON", "PROTEIN", "G", ["G"], ["FISH"], _SEAFOOD),
    ("TUNA", "PROTEIN", "G", ["G"], ["FISH"], _SEAFOOD),
    ("SHRIMP", "PROTEIN", "G", ["G"], ["SHELLFISH"], [*_SEAFOOD, "KOSHER"]),
    ("EGG", "PROTEIN", "G", ["G", "UNIT"], ["EGGS"], ["VEGAN"]),
    ("TOFU", "PROTEIN", "G", ["G"], ["SOY"], []),
    ("MILK", "DAIRY", "ML", ["ML", "CUP"], ["MILK"], _DAIRY),
    ("GREEK_YOGURT", "DAIRY", "G", ["G", "CUP"], ["MILK"], _DAIRY),
    ("FRESH_CHEESE", "DAIRY", "G", ["G"], ["MILK"], [*_DAIRY, "LOW_SODIUM"]),
    ("BANANA", "FRUIT", "G", ["G", "UNIT"], [], ["KETO", "LOW_SUGAR"]),
    ("APPLE", "FRUIT", "G", ["G", "UNIT"], [], ["KETO"]),
    ("ORANGE", "FRUIT", "G", ["G", "UNIT"], [], ["KETO"]),
    ("BERRIES", "FRUIT", "G", ["G", "CUP"], [], []),
    ("SPINACH", "VEGETABLE", "G", ["G", "CUP"], [], []),
    ("BROCCOLI", "VEGETABLE", "G", ["G", "CUP"], [], []),
    ("CARROT", "VEGETABLE", "G", ["G", "UNIT"], [], []),
    ("TOMATO", "VEGETABLE", "G", ["G", "UNIT"], [], []),
    ("BLACK_BEANS", "LEGUME", "G", ["G", "CUP"], [], ["KETO"]),
    ("LENTILS", "LEGUME", "G", ["G", "CUP"], [], ["KETO"]),
    ("CHICKPEAS", "LEGUME", "G", ["G", "CUP"], [], ["KETO"]),
    ("OLIVE_OIL", "FAT", "ML", ["ML", "TBSP"], [], []),
    ("AVOCADO", "FAT", "G", ["G", "UNIT"], [], []),
    ("PEANUT_BUTTER", "FAT", "G", ["G", "TBSP"], ["PEANUTS"], []),
    ("ALMONDS", "FAT", "G", ["G", "CUP"], ["TREE_NUTS"], []),
    ("TAHINI", "FAT", "G", ["G", "TBSP"], ["SESAME"], []),
    ("WATER", "BEVERAGE", "ML", ["ML", "CUP"], [], []),
    ("COFFEE", "BEVERAGE", "ML", ["ML", "CUP"], [], []),
    ("ORANGE_JUICE", "BEVERAGE", "ML", ["ML", "CUP"], [], ["LOW_SUGAR", "KETO"]),
    ("SPORTS_DRINK", "BEVERAGE", "ML", ["ML", "CUP"], [], ["LOW_SUGAR", "KETO"]),
    ("WHEY_PROTEIN", "SUPPLEMENT", "G", ["G", "TBSP"], ["MILK"], _DAIRY),
    ("PLANT_PROTEIN", "SUPPLEMENT", "G", ["G", "TBSP"], ["SOY"], []),
    ("ELECTROLYTES", "SUPPLEMENT", "G", ["G", "UNIT"], [], ["LOW_SODIUM"]),
]


def _create_schema() -> None:
    op.create_table(
        "food_catalog_item",
        sa.Column("food_code", sa.String(length=64), nullable=False),
        sa.Column("label_key", sa.String(length=80), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("default_unit", sa.String(length=8), nullable=False),
        sa.Column("allowed_units", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("allergens", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("incompatible_with", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("catalog_version", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("food_code", name=op.f("pk_food_catalog_item")),
    )
    op.create_table(
        "nutrition_plan",
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("professional_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("objectives", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("catalog_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("status IN ('ASSIGNED')", name=op.f("ck_nutrition_plan_status")),
        sa.CheckConstraint("version = 1", name=op.f("ck_nutrition_plan_version")),
        sa.CheckConstraint("start_date <= end_date", name=op.f("ck_nutrition_plan_period")),
        sa.PrimaryKeyConstraint("plan_id", name=op.f("pk_nutrition_plan")),
    )
    op.create_index(
        op.f("ix_nutrition_plan_athlete_id"), "nutrition_plan", ["athlete_id"], unique=False
    )
    op.create_index(
        op.f("ix_nutrition_plan_professional_id"),
        "nutrition_plan",
        ["professional_id"],
        unique=False,
    )
    op.create_table(
        "nutrition_plan_meal",
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("meal_type", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "meal_type IN ('BREAKFAST', 'LUNCH', 'DINNER', 'SNACK', 'HYDRATION')",
            name=op.f("ck_nutrition_plan_meal_meal_type"),
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["nutrition_plan.plan_id"],
            name=op.f("fk_nutrition_plan_meal_plan_id_nutrition_plan"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("plan_id", "position", name=op.f("pk_nutrition_plan_meal")),
    )
    op.create_table(
        "nutrition_plan_item",
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("meal_position", sa.SmallInteger(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("food_code", sa.String(length=64), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=5, scale=1), nullable=False),
        sa.Column("unit", sa.String(length=8), nullable=False),
        sa.CheckConstraint(
            "quantity BETWEEN 0.1 AND 5000.0", name=op.f("ck_nutrition_plan_item_quantity")
        ),
        sa.CheckConstraint(
            "unit IN ('G', 'ML', 'UNIT', 'CUP', 'TBSP')", name=op.f("ck_nutrition_plan_item_unit")
        ),
        sa.ForeignKeyConstraint(
            ["food_code"],
            ["food_catalog_item.food_code"],
            name=op.f("fk_nutrition_plan_item_food_code_food_catalog_item"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id", "meal_position"],
            ["nutrition_plan_meal.plan_id", "nutrition_plan_meal.position"],
            name=op.f("fk_nutrition_plan_item_plan_id_nutrition_plan_meal"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "plan_id", "meal_position", "position", name=op.f("pk_nutrition_plan_item")
        ),
    )


def seed_food_catalog(connection: sa.Connection) -> None:
    """Precarga idempotente (UPSERT): repetirla no duplica y restituye esta versión."""
    table = sa.Table("food_catalog_item", sa.MetaData(), autoload_with=connection)
    rows = [
        {
            "food_code": code,
            "label_key": f"food.{code.lower()}",
            "category": category,
            "default_unit": default_unit,
            "allowed_units": allowed_units,
            "allergens": allergens,
            "incompatible_with": incompatible_with,
            "catalog_version": CATALOG_VERSION,
        }
        for code, category, default_unit, allowed_units, allergens, incompatible_with in FOODS
    ]
    statement = postgresql.insert(table).values(rows)
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=["food_code"],
            set_={key: statement.excluded[key] for key in rows[0] if key != "food_code"},
        )
    )


def upgrade() -> None:
    _create_schema()
    seed_food_catalog(op.get_bind())


def downgrade() -> None:
    op.drop_table("nutrition_plan_item")
    op.drop_table("nutrition_plan_meal")
    op.drop_index(op.f("ix_nutrition_plan_professional_id"), table_name="nutrition_plan")
    op.drop_index(op.f("ix_nutrition_plan_athlete_id"), table_name="nutrition_plan")
    op.drop_table("nutrition_plan")
    op.drop_table("food_catalog_item")
