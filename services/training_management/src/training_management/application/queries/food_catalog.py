"""HU014 (ADR-037): catálogo de alimentos para cualquier actor autenticado.

El catálogo es dato de referencia precargado por migración; su `ETag` es la versión.
"""

from training_management.application.ports import Caller, FoodCatalogReader
from training_management.domain.nutrition.catalog import FoodCatalog


class GetFoodCatalog:
    def __init__(self, *, catalog: FoodCatalogReader) -> None:
        self._catalog = catalog

    def __call__(self, caller: Caller) -> FoodCatalog:
        # Cualquier actor autenticado: la identidad ya se verificó en el borde del servicio.
        return self._catalog.load()
