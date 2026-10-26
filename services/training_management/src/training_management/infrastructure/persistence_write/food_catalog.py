"""HU014: catálogo de alimentos (`food_catalog_item`) y su sonda de disponibilidad.

El catálogo es dato de referencia de la base de escritura, precargado por la migración
`0006_nutrition_plans`. El servicio lee la versión que su código conoce
(`FOOD_CATALOG_VERSION`) y no queda listo si falta esa versión o algún alimento esperado.
"""

import logging

from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from training_management.domain.nutrition.catalog import (
    FOOD_CATALOG,
    FOOD_CATALOG_VERSION,
    FoodCatalog,
    FoodCategory,
    FoodItem,
    FoodUnit,
)
from training_management.infrastructure.persistence_write.models import FoodCatalogItemRow

_logger = logging.getLogger("sportapp.catalog")


def _food(row: FoodCatalogItemRow) -> FoodItem:
    return FoodItem(
        food_code=row.food_code,
        category=FoodCategory(row.category),
        default_unit=FoodUnit(row.default_unit),
        allowed_units=tuple(FoodUnit(unit) for unit in row.allowed_units),
        allergens=tuple(row.allergens),
        incompatible_with=tuple(row.incompatible_with),
    )


class SqlAlchemyFoodCatalogReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def load(self) -> FoodCatalog:
        statement = (
            select(FoodCatalogItemRow)
            .where(FoodCatalogItemRow.catalog_version == FOOD_CATALOG_VERSION)
            .order_by(FoodCatalogItemRow.food_code)
        )
        with self._session_factory() as session:
            items = tuple(_food(row) for row in session.scalars(statement))
        return FoodCatalog(catalog_version=FOOD_CATALOG_VERSION, items=items)


class FoodCatalogReadinessCheck:
    """El servicio no queda listo con el catálogo ausente, parcial o de otra versión."""

    name = "food_catalog"

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def __call__(self) -> bool:
        try:
            problem = self._find_problem()
        except SQLAlchemyError as error:
            problem = f"database_error:{type(error).__name__}"
        if problem is None:
            return True
        _logger.warning(
            "food_catalog.not_ready",
            extra={"fields": {"expected_version": FOOD_CATALOG_VERSION, "problem": problem}},
        )
        return False

    def _find_problem(self) -> str | None:
        with self._engine.connect() as connection:
            installed = set(
                connection.scalars(
                    select(FoodCatalogItemRow.food_code).where(
                        FoodCatalogItemRow.catalog_version == FOOD_CATALOG_VERSION
                    )
                )
            )
        if not installed:
            return "catalog_version_missing"
        if not FOOD_CATALOG.codes <= installed:
            return "food_missing"
        return None
