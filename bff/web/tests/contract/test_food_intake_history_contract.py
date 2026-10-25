"""HU013 (ADR-037): `bff-web` y `training-management` publican las dos consultas del historial
de alimentación con los mismos parámetros, códigos de respuesta y esquemas de éxito.

Compara los OpenAPI versionados: el BFF reenvía las respuestas del servicio sin
reinterpretarlas, así que una diferencia sería una API paralela. Además, la consulta propia
convive en la misma ruta con el registro de HU043 (`POST`, solo en el servicio) y el BFF web
conserva las consultas de HU014 que completan la vista (planes y catálogo).
"""

import unittest
from typing import Any

from test_event_publication_contract import _document, _resolve

OPERATIONS = (
    ("/v1/me/food-intakes", "get"),
    ("/v1/athletes/{athlete_id}/food-intakes", "get"),
)
PARAMETERS = {"from", "to", "meal_type", "limit", "cursor", "authorization"}


def _success_schema(document: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    content = document["paths"][path][method]["responses"]["200"]["content"]
    return _resolve(document, content["application/json"]["schema"])


class FoodIntakeHistoryContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bff = _document("bff-web")
        self.service = _document("training-management")

    def test_same_operations_parameters_and_response_codes(self) -> None:
        for path, method in OPERATIONS:
            with self.subTest(operation=f"{method.upper()} {path}"):
                bff, service = self.bff["paths"][path][method], self.service["paths"][path][method]
                names = {p["name"] for p in service.get("parameters", [])}
                self.assertEqual({p["name"] for p in bff.get("parameters", [])}, names)
                self.assertLessEqual(PARAMETERS, {name.lower() for name in names})
                self.assertLessEqual({"200", "401", "403", "422"}, set(service["responses"]))
                # El BFF agrega sus propios `504 upstream_timeout` (y `503` del transporte).
                self.assertLessEqual(set(service["responses"]), set(bff["responses"]))

    def test_success_schemas_are_identical(self) -> None:
        for path, method in OPERATIONS:
            with self.subTest(operation=f"{method.upper()} {path}"):
                self.assertEqual(
                    _success_schema(self.bff, path, method),
                    _success_schema(self.service, path, method),
                )

    def test_items_carry_food_code_quantity_unit_meal_and_date_only(self) -> None:
        """CA1: alimento, cantidad, unidad, tipo de comida y fecha; sin nutrientes."""
        page = _success_schema(self.service, *OPERATIONS[0])
        intake = _resolve(self.service, page["properties"]["items"]["items"])
        item = _resolve(self.service, intake["properties"]["items"]["items"])

        self.assertEqual(set(item["properties"]), {"food_code", "quantity", "unit"})
        self.assertLessEqual({"consumed_at", "meal_type", "intake_id"}, set(intake["properties"]))
        self.assertFalse({"calories", "kcal", "nutrients"} & set(intake["properties"]))
        self.assertEqual(set(page["properties"]), {"items", "next_cursor"})

    def test_own_route_keeps_the_hu043_registration_in_the_service(self) -> None:
        self.assertEqual(set(self.service["paths"]["/v1/me/food-intakes"]), {"get", "post"})
        self.assertEqual(set(self.bff["paths"]["/v1/me/food-intakes"]), {"get"})

    def test_bff_web_keeps_plans_and_catalog_for_the_nutrition_view(self) -> None:
        for path in ("/v1/me/nutrition-plans", "/v1/food-catalog"):
            with self.subTest(path=path):
                self.assertIn("get", self.bff["paths"][path])


if __name__ == "__main__":
    unittest.main()
