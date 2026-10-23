"""HU014: catálogo de alimentos, reglas del plan y bloqueos por alergia o restricción
(dominio puro, sin infraestructura)."""

import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from nutrition_fakes import NO_HABITS, habits

from training_management.domain.nutrition.catalog import (
    FOOD_CATALOG,
    FOOD_CATALOG_VERSION,
    FoodUnit,
)
from training_management.domain.nutrition.events import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    nutrition_plan_created_payload,
)
from training_management.domain.nutrition.model import (
    CONFLICTS_WITH_ALLERGY,
    CONFLICTS_WITH_RESTRICTION,
    InvalidNutritionPlan,
    Meal,
    MealType,
    NutritionPlan,
    NutritionPlanStatus,
    PlanItem,
    format_quantity,
    quantity_in_range,
)

NOW = datetime(2026, 10, 22, 12, 0, 0, 123456, tzinfo=UTC)
# Catálogos cerrados de ADR-035.
ALLERGIES = {"PEANUTS", "TREE_NUTS", "MILK", "EGGS", "WHEAT", "SOY", "FISH", "SHELLFISH", "SESAME"}
DIET_PATTERNS = {"VEGETARIAN", "VEGAN", "PESCATARIAN"}
RESTRICTIONS = {"GLUTEN_FREE", "LACTOSE_FREE", "LOW_SODIUM", "LOW_SUGAR", "KETO", "HALAL", "KOSHER"}


def meal(*items: PlanItem, meal_type: MealType = MealType.LUNCH) -> Meal:
    return Meal(meal_type=meal_type, items=items)


def food(code: str, quantity: str = "100", unit: str = "G") -> PlanItem:
    return PlanItem(food_code=code, quantity=Decimal(quantity), unit=unit)


def create(**overrides: Any) -> NutritionPlan:
    values: dict[str, Any] = {
        "plan_id": uuid4(),
        "athlete_id": uuid4(),
        "professional_id": uuid4(),
        "title": "  Plan   de recuperación ",
        "objectives": ["Hidratación  diaria"],
        "start_date": date(2026, 11, 2),
        "end_date": date(2026, 11, 29),
        "meals": [meal(food("OATS", "80"), meal_type=MealType.BREAKFAST)],
        "catalog": FOOD_CATALOG,
        "now": NOW,
    }
    values.update(overrides)
    return NutritionPlan.create(**values)


def issues(error: InvalidNutritionPlan) -> list[tuple[str, str]]:
    return [(item.field, item.issue) for item in error.violations]


class FoodCatalogTest(unittest.TestCase):
    def test_catalog_lists_every_proposed_food_once(self) -> None:
        codes = [item.food_code for item in FOOD_CATALOG.items]

        self.assertEqual(FOOD_CATALOG.catalog_version, FOOD_CATALOG_VERSION)
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(len(codes), 41)
        self.assertEqual(FOOD_CATALOG.etag, '"food-catalog-2026-10-13.v1"')

    def test_units_allergens_and_incompatibilities_use_closed_catalogs(self) -> None:
        for item in FOOD_CATALOG.items:
            with self.subTest(food=item.food_code):
                self.assertIn(item.default_unit, item.allowed_units)
                self.assertTrue(set(item.allergens) <= ALLERGIES)
                self.assertTrue(set(item.incompatible_with) <= DIET_PATTERNS | RESTRICTIONS)
                self.assertNotIn("OMNIVORE", item.incompatible_with)
                self.assertNotIn("FLEXITARIAN", item.incompatible_with)
                self.assertEqual(item.label_key, f"food.{item.food_code.lower()}")

    def test_solids_use_grams_and_liquids_milliliters(self) -> None:
        water, oats = FOOD_CATALOG.find("WATER"), FOOD_CATALOG.find("OATS")
        assert water is not None
        assert oats is not None

        self.assertEqual(water.default_unit, FoodUnit.ML)
        self.assertEqual(oats.default_unit, FoodUnit.G)
        self.assertTrue(oats.allows("CUP"))
        self.assertFalse(water.allows("G"))
        self.assertIsNone(FOOD_CATALOG.find("PIZZA"))


class NutritionPlanRulesTest(unittest.TestCase):
    def test_creates_assigned_plan_in_version_one_with_normalized_values(self) -> None:
        plan = create(
            meals=[
                meal(food("OATS", "80"), food("BANANA", "1", "UNIT"), meal_type=MealType.BREAKFAST),
                meal(food("WATER", "2000.0", "ML"), meal_type=MealType.HYDRATION),
            ]
        )

        self.assertEqual((plan.status, plan.version), (NutritionPlanStatus.ASSIGNED, 1))
        self.assertEqual(plan.catalog_version, FOOD_CATALOG_VERSION)
        self.assertEqual(plan.title, "Plan de recuperación")
        self.assertEqual(plan.objectives, ("Hidratación diaria",))
        self.assertEqual(
            [m.meal_type for m in plan.meals], [MealType.BREAKFAST, MealType.HYDRATION]
        )
        self.assertEqual(format_quantity(plan.meals[0].items[0].quantity), "80.0")
        self.assertEqual(format_quantity(plan.meals[1].items[0].quantity), "2000.0")

    def test_period_limits(self) -> None:
        start = date(2026, 11, 2)
        last_day = start + timedelta(days=181)  # 182 días inclusivos (26 semanas)

        self.assertEqual(create(start_date=start, end_date=start).end_date, start)
        self.assertEqual(create(start_date=start, end_date=last_day).end_date, last_day)
        with self.assertRaises(InvalidNutritionPlan) as too_long:
            create(start_date=start, end_date=last_day + timedelta(days=1))
        with self.assertRaises(InvalidNutritionPlan) as reversed_period:
            create(start_date=start, end_date=start - timedelta(days=1))

        self.assertEqual(issues(too_long.exception), [("end_date", "period_too_long")])
        self.assertEqual(
            issues(reversed_period.exception), [("end_date", "end_date_before_start_date")]
        )

    def test_quantity_limits_and_precision(self) -> None:
        for value, valid in (
            ("0.1", True),
            ("5000.0", True),
            ("5000", True),
            ("0.0", False),
            ("0.05", False),
            ("5000.1", False),
            ("1.25", False),
            ("-1", False),
        ):
            with self.subTest(quantity=value):
                self.assertEqual(quantity_in_range(Decimal(value)), valid)
        self.assertFalse(quantity_in_range(Decimal("NaN")))
        self.assertFalse(quantity_in_range(Decimal("Infinity")))

    def test_reports_every_item_rule_at_once(self) -> None:
        with self.assertRaises(InvalidNutritionPlan) as error:
            create(
                meals=[
                    meal(food("PIZZA"), food("WATER", "100", "G")),
                    meal(food("OATS", "5000.5"), food("MILK", "0", "TBSP")),
                ]
            )

        self.assertEqual(
            issues(error.exception),
            [
                ("meals.0.items.0.food_code", "unknown_value"),
                ("meals.0.items.1.unit", "unit_not_allowed"),
                ("meals.1.items.0.quantity", "out_of_range"),
                ("meals.1.items.1.unit", "unit_not_allowed"),
                ("meals.1.items.1.quantity", "out_of_range"),
            ],
        )
        self.assertFalse(error.exception.blocked)
        self.assertEqual(error.exception.code, "nutrition_plan_invalid")

    def test_shape_limits_are_invariants(self) -> None:
        nine_meals = [meal(food("OATS"))] * 9
        sixteen_items = [meal(*[food("OATS")] * 16)]
        for overrides in (
            {"title": "x" * 121},
            {"title": "   "},
            {"objectives": []},
            {"objectives": ["a"] * 6},
            {"objectives": ["x" * 201]},
            {"meals": []},
            {"meals": nine_meals},
            {"meals": sixteen_items},
        ):
            with self.subTest(overrides=list(overrides)), self.assertRaises(ValueError):
                create(**overrides)
        self.assertEqual(len(create(meals=[meal(food("OATS"))] * 8).meals), 8)
        self.assertEqual(len(create(meals=[meal(*[food("OATS")] * 15)]).meals[0].items), 15)


class NutritionConflictsTest(unittest.TestCase):
    def conflicts(self, *items: PlanItem, restrictions: Any = NO_HABITS) -> list[tuple[str, str]]:
        plan = create(meals=[meal(*items)])
        return [(item.field, item.issue) for item in plan.conflicts(FOOD_CATALOG, restrictions)]

    def test_vegan_blocks_chicken_breast(self) -> None:
        self.assertEqual(
            self.conflicts(food("OATS"), food("CHICKEN_BREAST"), restrictions=habits(diet="VEGAN")),
            [("meals.0.items.1.food_code", CONFLICTS_WITH_RESTRICTION)],
        )

    def test_gluten_free_blocks_pasta(self) -> None:
        self.assertEqual(
            self.conflicts(food("PASTA"), restrictions=habits(restrictions=("GLUTEN_FREE",))),
            [("meals.0.items.0.food_code", CONFLICTS_WITH_RESTRICTION)],
        )

    def test_peanut_allergy_blocks_peanut_butter(self) -> None:
        self.assertEqual(
            self.conflicts(
                food("PEANUT_BUTTER", "30"), restrictions=habits(allergies=("PEANUTS",))
            ),
            [("meals.0.items.0.food_code", CONFLICTS_WITH_ALLERGY)],
        )

    def test_allergy_and_restriction_on_the_same_food_give_two_details(self) -> None:
        both = habits(diet="VEGAN", restrictions=("LACTOSE_FREE",), allergies=("MILK", "SOY"))

        self.assertEqual(
            self.conflicts(food("MILK", "200", "ML"), restrictions=both),
            [
                ("meals.0.items.0.food_code", CONFLICTS_WITH_ALLERGY),
                ("meals.0.items.0.food_code", CONFLICTS_WITH_RESTRICTION),
            ],
        )

    def test_compatible_foods_and_missing_habits_do_not_block(self) -> None:
        self.assertEqual(self.conflicts(food("CHICKEN_BREAST"), food("PASTA")), [])
        self.assertEqual(
            self.conflicts(food("CHICKEN_BREAST"), restrictions=habits(diet="FLEXITARIAN")), []
        )
        self.assertTrue(NO_HABITS.habits_missing)
        self.assertFalse(habits().habits_missing)


class NutritionEventTest(unittest.TestCase):
    def test_payload_is_the_full_plan_without_sensitive_data(self) -> None:
        plan = create(meals=[meal(food("OATS", "80"), meal_type=MealType.BREAKFAST)])

        payload = nutrition_plan_created_payload(plan)

        self.assertEqual(
            (EVENT_TYPE, AGGREGATE_TYPE, SCHEMA_VERSION),
            ("plan.alimentacion_creado.v1", "nutrition_plan", 1),
        )
        self.assertEqual(payload["plan_id"], str(plan.plan_id))
        self.assertEqual(payload["status"], "ASSIGNED")
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["catalog_version"], FOOD_CATALOG_VERSION)
        self.assertEqual(
            payload["meals"],
            [
                {
                    "meal_type": "BREAKFAST",
                    "items": [{"food_code": "OATS", "quantity": "80.0", "unit": "G"}],
                }
            ],
        )
        self.assertEqual(payload["created_at"], "2026-10-22T12:00:00.123456Z")
        self.assertNotIn("warnings", payload)
        self.assertNotIn("allergies", payload)
        self.assertNotIn("restrictions", payload)


if __name__ == "__main__":
    unittest.main()
