"""HU030 (ADR-041): reglas del agregado `NutritionRoutineTemplate`, coherencia con el catálogo
de alimentos, reconstrucción de su documento y reglas de sugerencia (exclusión por alérgeno y
patrón, motivos, orden y lista no personalizada)."""

import unittest
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

from fakes import NOW
from routine_fakes import allergens, meal, routine_input

from training_management.application.routine_documents import (
    InvalidRoutineDocument,
    routine_from_document,
)
from training_management.domain.nutrition.catalog import FOOD_CATALOG
from training_management.domain.nutrition.model import MealType
from training_management.domain.plans.errors import Violation
from training_management.domain.plans.model import Sport
from training_management.domain.routines.events import (
    EVENT_TYPE_BY_STATUS,
    PUBLISHED_EVENT_TYPE,
    RETIRED_EVENT_TYPE,
    routine_document,
)
from training_management.domain.routines.model import (
    Allergy,
    DietPattern,
    InvalidRoutine,
    NutritionRoutineTemplate,
    RoutineItem,
    RoutineMeal,
    RoutineStatus,
)
from training_management.domain.routines.suggestions import (
    RoutineReason,
    RoutineSuggestionContext,
    is_compatible,
    reasons_for,
    suggestion_order,
)
from training_management.domain.templates.model import Goal


def publish(**overrides: object) -> NutritionRoutineTemplate:
    return NutritionRoutineTemplate.publish(
        template_id=uuid4(),
        professional_id=uuid4(),
        data=routine_input(**overrides),
        catalog=FOOD_CATALOG,
        now=NOW,
    )


def violations(**overrides: object) -> tuple[Violation, ...]:
    try:
        publish(**overrides)
    except InvalidRoutine as error:
        return error.violations
    raise AssertionError("La rutina debía rechazarse.")


class RoutinePublicationTest(unittest.TestCase):
    def test_publishes_version_one_with_normalized_text(self) -> None:
        routine = publish(title="  Desayunos   de fondo ", description="  Semana de carga  ")

        self.assertEqual(
            (routine.status, routine.version, routine.title, routine.description),
            (RoutineStatus.PUBLISHED, 1, "Desayunos de fondo", "Semana de carga"),
        )
        self.assertEqual((routine.created_at, routine.published_at), (NOW, NOW))
        self.assertIsNone(routine.retired_at)

    def test_allergens_include_the_catalog_ones_in_catalog_order(self) -> None:
        """La exclusión del deportista nunca depende de una omisión del profesional."""
        routine = publish(
            contains_allergens=allergens("SESAME"),
            daily_meals=meal("WHOLE_WHEAT_BREAD", "GREEK_YOGURT"),
        )

        self.assertEqual(routine.contains_allergens, (Allergy.MILK, Allergy.WHEAT, Allergy.SESAME))

    def test_unknown_food_code_is_rejected_at_its_position(self) -> None:
        """CA4: `food_code` inexistente."""
        found = violations(
            daily_meals=(
                RoutineMeal(MealType.BREAKFAST, (RoutineItem("OATS", 1),)),
                RoutineMeal(MealType.DINNER, (RoutineItem("OATS", 1), RoutineItem("PIZZA", 1))),
            )
        )

        self.assertEqual(found, (Violation("daily_meals.1.items.1.food_code", "unknown_value"),))

    def test_food_incompatible_with_the_declared_pattern_is_rejected(self) -> None:
        cases = {
            DietPattern.VEGAN: "EGG",
            DietPattern.VEGETARIAN: "SALMON",
            DietPattern.PESCATARIAN: "CHICKEN_BREAST",
        }
        for pattern, code in cases.items():
            with self.subTest(pattern=pattern):
                self.assertEqual(
                    violations(dietary_pattern=pattern, daily_meals=meal(code)),
                    (Violation("daily_meals.0.items.0.food_code", "conflicts_with_restriction"),),
                )
        omnivore = publish(dietary_pattern=DietPattern.OMNIVORE, daily_meals=meal("BEEF"))
        self.assertEqual(omnivore.dietary_pattern, DietPattern.OMNIVORE)

    def test_reports_every_broken_rule(self) -> None:
        found = violations(
            title="ab",
            description="   ",
            contains_allergens=allergens("FISH", "FISH"),
            duration_days=91,
            daily_meals=(RoutineMeal(MealType.LUNCH, (RoutineItem("OATS", 11),)),) * 9,
        )
        fields = {(item.field, item.issue) for item in found}

        self.assertTrue(
            {
                ("title", "invalid_length"),
                ("description", "invalid_length"),
                ("contains_allergens", "duplicate_value"),
                ("duration_days", "out_of_range"),
                ("daily_meals", "out_of_range"),
                ("daily_meals.0.items.0.portions", "out_of_range"),
            }
            <= fields
        )

    def test_meal_without_items_and_zero_days_are_rejected(self) -> None:
        found = violations(duration_days=0, daily_meals=(RoutineMeal(MealType.SNACK, ()),))

        self.assertEqual(
            set(found),
            {
                Violation("duration_days", "out_of_range"),
                Violation("daily_meals.0.items", "out_of_range"),
            },
        )

    def test_retire_raises_the_version_once(self) -> None:
        routine = publish()
        later = NOW + timedelta(hours=2)

        retired = routine.retire(later)

        self.assertEqual(
            (retired.status, retired.version, retired.retired_at, retired.updated_at),
            (RoutineStatus.RETIRED, 2, later, later),
        )
        with self.assertRaises(ValueError):
            retired.retire(later)


class RoutineDocumentTest(unittest.TestCase):
    def test_document_round_trip_and_event_types(self) -> None:
        routine = publish(sport=Sport.CYCLING)
        retired = routine.retire(NOW + timedelta(days=1))

        for item in (routine, retired):
            self.assertEqual(routine_from_document(routine_document(item)), item)
        self.assertEqual(EVENT_TYPE_BY_STATUS[routine.status], PUBLISHED_EVENT_TYPE)
        self.assertEqual(EVENT_TYPE_BY_STATUS[retired.status], RETIRED_EVENT_TYPE)
        self.assertEqual(routine_document(routine)["published_at"], "2026-10-12T12:00:00Z")

    def test_invalid_documents_name_only_the_reason(self) -> None:
        document = routine_document(publish(contains_allergens=allergens("FISH", "MILK")))
        cases = {
            "contains_allergens_invalid": {**document, "contains_allergens": ["FISH", "MILK"]},
            "dietary_pattern_invalid": {**document, "dietary_pattern": "PALEO"},
            "daily_meals_invalid": {**document, "daily_meals": []},
            "duration_days_invalid": {**document, "duration_days": 0},
            "retired_at_mismatch": {**document, "status": "RETIRED"},
            "sport_invalid": {**document, "sport": "golf"},
        }
        for reason, broken in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaises(InvalidRoutineDocument) as error:
                    routine_from_document(broken)
                self.assertEqual(error.exception.reason, reason)


class RoutineSuggestionRulesTest(unittest.TestCase):
    def test_allergen_of_the_athlete_excludes_the_routine(self) -> None:
        """CA2: una alergia conocida excluye la rutina."""
        context = RoutineSuggestionContext(
            dietary_pattern=DietPattern.OMNIVORE, exclude_allergens=allergens("FISH")
        )

        self.assertFalse(
            is_compatible(
                context,
                dietary_pattern=DietPattern.PESCATARIAN,
                contains_allergens=allergens("MILK", "FISH"),
            )
        )
        self.assertTrue(
            is_compatible(
                context,
                dietary_pattern=DietPattern.PESCATARIAN,
                contains_allergens=allergens("MILK"),
            )
        )

    def test_pattern_compatibility_accepts_only_equal_or_stricter(self) -> None:
        """CA2: un patrón incompatible excluye la rutina."""
        accepted = {
            DietPattern.OMNIVORE: set(DietPattern),
            DietPattern.FLEXITARIAN: set(DietPattern),
            DietPattern.PESCATARIAN: {
                DietPattern.PESCATARIAN,
                DietPattern.VEGETARIAN,
                DietPattern.VEGAN,
            },
            DietPattern.VEGETARIAN: {DietPattern.VEGETARIAN, DietPattern.VEGAN},
            DietPattern.VEGAN: {DietPattern.VEGAN},
        }
        for athlete, expected in accepted.items():
            context = RoutineSuggestionContext(dietary_pattern=athlete)
            for routine in DietPattern:
                with self.subTest(athlete=athlete, routine=routine):
                    self.assertEqual(
                        is_compatible(context, dietary_pattern=routine, contains_allergens=()),
                        routine in expected,
                    )

    def test_not_personalized_without_nutrition_habits(self) -> None:
        """CA3: sin HU009 nada se verifica ni se marca compatible."""
        context = RoutineSuggestionContext(sports=(Sport.RUNNING,), primary_goal=Goal.PERFORMANCE)

        self.assertFalse(context.personalized)
        self.assertTrue(
            is_compatible(
                context, dietary_pattern=DietPattern.OMNIVORE, contains_allergens=allergens("FISH")
            )
        )
        self.assertEqual(reasons_for(context, goal=Goal.PERFORMANCE, sport=Sport.RUNNING), ())

    def test_reasons(self) -> None:
        context = RoutineSuggestionContext(
            dietary_pattern=DietPattern.VEGAN,
            sports=(Sport.RUNNING, Sport.CYCLING),
            primary_goal=Goal.HEALTH,
        )
        compatible = RoutineReason.RESTRICTION_COMPATIBLE

        self.assertEqual(
            reasons_for(context, goal=Goal.HEALTH, sport=Sport.CYCLING),
            (compatible, RoutineReason.GOAL_MATCH, RoutineReason.SPORT_MATCH),
        )
        self.assertEqual(reasons_for(context, goal=Goal.RECREATION, sport=None), (compatible,))
        self.assertEqual(
            reasons_for(context, goal=Goal.RECREATION, sport=Sport.RUNNING),
            (compatible, RoutineReason.SPORT_MATCH),
        )

    def test_order_goal_then_sport_then_most_recent(self) -> None:
        """CA1: prioriza objetivo y deporte; empates por `published_at` y `template_id` desc."""
        context = RoutineSuggestionContext(
            dietary_pattern=DietPattern.OMNIVORE,
            sports=(Sport.RUNNING,),
            primary_goal=Goal.HEALTH,
        )
        low, high = UUID(int=1), UUID(int=2)
        candidates = {
            "sport_only": (Goal.RECREATION, Sport.RUNNING, NOW, low),
            "goal_old": (Goal.HEALTH, None, NOW - timedelta(days=3), low),
            "both": (Goal.HEALTH, Sport.RUNNING, NOW - timedelta(days=9), low),
            "none_new": (Goal.RECREATION, None, NOW, high),
            "none_tie": (Goal.RECREATION, None, NOW, low),
            "goal_new": (Goal.HEALTH, Sport.SWIMMING, NOW, low),
        }
        ordered = sorted(
            candidates,
            key=lambda name: suggestion_order(
                context,
                goal=candidates[name][0],
                sport=candidates[name][1],
                published_at=candidates[name][2],
                template_id=candidates[name][3],
            ),
        )

        self.assertEqual(
            ordered, ["both", "goal_new", "goal_old", "sport_only", "none_new", "none_tie"]
        )
        unpersonalized = RoutineSuggestionContext(sports=(Sport.RUNNING,))
        self.assertLess(
            suggestion_order(
                unpersonalized, goal=Goal.HEALTH, sport=None, published_at=NOW, template_id=low
            ),
            suggestion_order(
                unpersonalized,
                goal=Goal.HEALTH,
                sport=Sport.RUNNING,
                published_at=NOW - timedelta(seconds=1),
                template_id=high,
            ),
        )

    def test_retired_routine_keeps_its_data(self) -> None:
        routine = publish()
        retired = routine.retire(NOW)

        restored = replace(
            retired, status=routine.status, version=1, retired_at=None, updated_at=NOW
        )
        self.assertEqual(restored, routine)


if __name__ == "__main__":
    unittest.main()
