"""Catálogo cerrado de alimentos `food-catalog-2026-10-13.v1` (ADR-037, HU014 y HU043).

Propuesta del 2026-10-22 pendiente de ratificar por el responsable en la ficha de HU014:
- `allergens` usa solo el catálogo de alergias de ADR-035;
- `incompatible_with` usa solo valores de `diet_pattern` y `restrictions` de ADR-035
  (`OMNIVORE` y `FLEXITARIAN` nunca bloquean); la incompatibilidad se comprueba con estos
  datos, sin inferencias;
- sólidos en `G` (más `UNIT`, `CUP` o `TBSP` cuando tiene sentido); líquidos en `ML`.

Los datos viven en la base de escritura (`food_catalog_item`), precargados por la migración
`0006_nutrition_plans`; ampliar el catálogo es otra migración con otra versión, no un CRUD.
Esta lista es la que el código espera encontrar: la sonda de disponibilidad la compara.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

FOOD_CATALOG_VERSION = "food-catalog-2026-10-13.v1"
LABEL_KEY_PREFIX = "food."


class FoodCategory(StrEnum):
    GRAIN = "GRAIN"
    PROTEIN = "PROTEIN"
    DAIRY = "DAIRY"
    FRUIT = "FRUIT"
    VEGETABLE = "VEGETABLE"
    LEGUME = "LEGUME"
    FAT = "FAT"
    BEVERAGE = "BEVERAGE"
    SUPPLEMENT = "SUPPLEMENT"


class FoodUnit(StrEnum):
    G = "G"
    ML = "ML"
    UNIT = "UNIT"
    CUP = "CUP"
    TBSP = "TBSP"


@dataclass(frozen=True)
class FoodItem:
    food_code: str
    category: FoodCategory
    default_unit: FoodUnit
    allowed_units: tuple[FoodUnit, ...]
    allergens: tuple[str, ...] = ()
    incompatible_with: tuple[str, ...] = ()

    @property
    def label_key(self) -> str:
        return f"{LABEL_KEY_PREFIX}{self.food_code.lower()}"

    def allows(self, unit: str) -> bool:
        return unit in {item.value for item in self.allowed_units}


@dataclass(frozen=True)
class FoodCatalog:
    """Catálogo versionado e inmutable; el `ETag` es su versión."""

    catalog_version: str
    items: tuple[FoodItem, ...]

    @property
    def etag(self) -> str:
        return f'"{self.catalog_version}"'

    def find(self, food_code: str) -> FoodItem | None:
        return next((item for item in self.items if item.food_code == food_code), None)

    @property
    def codes(self) -> frozenset[str]:
        return frozenset(item.food_code for item in self.items)


_G = FoodUnit.G
_ML = FoodUnit.ML
_UNIT = FoodUnit.UNIT
_CUP = FoodUnit.CUP
_TBSP = FoodUnit.TBSP


def _solid(
    code: str,
    category: FoodCategory,
    extra_units: Iterable[FoodUnit] = (),
    allergens: tuple[str, ...] = (),
    incompatible_with: tuple[str, ...] = (),
) -> FoodItem:
    return FoodItem(code, category, _G, (_G, *extra_units), allergens, incompatible_with)


def _liquid(
    code: str,
    category: FoodCategory,
    extra_units: Iterable[FoodUnit] = (_CUP,),
    allergens: tuple[str, ...] = (),
    incompatible_with: tuple[str, ...] = (),
) -> FoodItem:
    return FoodItem(code, category, _ML, (_ML, *extra_units), allergens, incompatible_with)


_GRAIN = FoodCategory.GRAIN
_PROTEIN = FoodCategory.PROTEIN
_DAIRY = FoodCategory.DAIRY
_FRUIT = FoodCategory.FRUIT
_VEGETABLE = FoodCategory.VEGETABLE
_LEGUME = FoodCategory.LEGUME
_FAT = FoodCategory.FAT
_BEVERAGE = FoodCategory.BEVERAGE
_SUPPLEMENT = FoodCategory.SUPPLEMENT

_MEAT = ("VEGAN", "VEGETARIAN", "PESCATARIAN")
_SEAFOOD = ("VEGAN", "VEGETARIAN")
_DAIRY_BLOCKS = ("VEGAN", "LACTOSE_FREE")

FOOD_ITEMS: tuple[FoodItem, ...] = (
    _solid("OATS", _GRAIN, (_CUP, _TBSP)),
    _solid("WHITE_RICE", _GRAIN, (_CUP,)),
    _solid("BROWN_RICE", _GRAIN, (_CUP,)),
    _solid("QUINOA", _GRAIN, (_CUP,)),
    _solid("WHOLE_WHEAT_BREAD", _GRAIN, (_UNIT,), ("WHEAT",), ("GLUTEN_FREE", "KETO")),
    _solid("PASTA", _GRAIN, (_CUP,), ("WHEAT",), ("GLUTEN_FREE", "KETO")),
    _solid("CORN_AREPA", _GRAIN, (_UNIT,), (), ("KETO",)),
    _solid("CHICKEN_BREAST", _PROTEIN, (), (), _MEAT),
    _solid("BEEF", _PROTEIN, (), (), _MEAT),
    _solid("PORK_LOIN", _PROTEIN, (), (), (*_MEAT, "HALAL", "KOSHER")),
    _solid("SALMON", _PROTEIN, (), ("FISH",), _SEAFOOD),
    _solid("TUNA", _PROTEIN, (), ("FISH",), _SEAFOOD),
    _solid("SHRIMP", _PROTEIN, (), ("SHELLFISH",), (*_SEAFOOD, "KOSHER")),
    _solid("EGG", _PROTEIN, (_UNIT,), ("EGGS",), ("VEGAN",)),
    _solid("TOFU", _PROTEIN, (), ("SOY",)),
    _liquid("MILK", _DAIRY, (_CUP,), ("MILK",), _DAIRY_BLOCKS),
    _solid("GREEK_YOGURT", _DAIRY, (_CUP,), ("MILK",), _DAIRY_BLOCKS),
    _solid("FRESH_CHEESE", _DAIRY, (), ("MILK",), (*_DAIRY_BLOCKS, "LOW_SODIUM")),
    _solid("BANANA", _FRUIT, (_UNIT,), (), ("KETO", "LOW_SUGAR")),
    _solid("APPLE", _FRUIT, (_UNIT,), (), ("KETO",)),
    _solid("ORANGE", _FRUIT, (_UNIT,), (), ("KETO",)),
    _solid("BERRIES", _FRUIT, (_CUP,)),
    _solid("SPINACH", _VEGETABLE, (_CUP,)),
    _solid("BROCCOLI", _VEGETABLE, (_CUP,)),
    _solid("CARROT", _VEGETABLE, (_UNIT,)),
    _solid("TOMATO", _VEGETABLE, (_UNIT,)),
    _solid("BLACK_BEANS", _LEGUME, (_CUP,), (), ("KETO",)),
    _solid("LENTILS", _LEGUME, (_CUP,), (), ("KETO",)),
    _solid("CHICKPEAS", _LEGUME, (_CUP,), (), ("KETO",)),
    _liquid("OLIVE_OIL", _FAT, (_TBSP,)),
    _solid("AVOCADO", _FAT, (_UNIT,)),
    _solid("PEANUT_BUTTER", _FAT, (_TBSP,), ("PEANUTS",)),
    _solid("ALMONDS", _FAT, (_CUP,), ("TREE_NUTS",)),
    _solid("TAHINI", _FAT, (_TBSP,), ("SESAME",)),
    _liquid("WATER", _BEVERAGE),
    _liquid("COFFEE", _BEVERAGE),
    _liquid("ORANGE_JUICE", _BEVERAGE, (_CUP,), (), ("LOW_SUGAR", "KETO")),
    _liquid("SPORTS_DRINK", _BEVERAGE, (_CUP,), (), ("LOW_SUGAR", "KETO")),
    _solid("WHEY_PROTEIN", _SUPPLEMENT, (_TBSP,), ("MILK",), _DAIRY_BLOCKS),
    _solid("PLANT_PROTEIN", _SUPPLEMENT, (_TBSP,), ("SOY",)),
    _solid("ELECTROLYTES", _SUPPLEMENT, (_UNIT,), (), ("LOW_SODIUM",)),
)

FOOD_CATALOG = FoodCatalog(catalog_version=FOOD_CATALOG_VERSION, items=FOOD_ITEMS)
