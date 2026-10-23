"""HU014 (ADR-037): `bff-web` y `training-management` publican las cuatro operaciones de
alimentación con los mismos parámetros, códigos de respuesta y esquemas de éxito.

Compara los OpenAPI versionados: el BFF reenvía las respuestas del servicio sin
reinterpretarlas, así que una diferencia sería una API paralela. El único cambio permitido
es el cuerpo del `POST`: la web identifica al deportista por `athlete_email` y el servicio,
por `athlete_id` (el BFF lo resuelve con el contrato interno de Usuarios, como en HU010).
"""

import unittest
from typing import Any

from test_event_publication_contract import _document, _resolve

OPERATIONS = (
    ("/v1/nutrition-plans", "post", "201"),
    ("/v1/nutrition-plans/{plan_id}", "get", "200"),
    ("/v1/me/nutrition-plans", "get", "200"),
    ("/v1/food-catalog", "get", "200"),
)
POST_PATH = OPERATIONS[0][0]


def _success_schema(
    document: dict[str, Any], path: str, method: str, status: str
) -> dict[str, Any]:
    content = document["paths"][path][method]["responses"][status]["content"]
    return _resolve(document, content["application/json"]["schema"])


def _request_schema(document: dict[str, Any]) -> dict[str, Any]:
    content = document["paths"][POST_PATH]["post"]["requestBody"]["content"]
    return _resolve(document, content["application/json"]["schema"])


class NutritionContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bff = _document("bff-web")
        self.service = _document("training-management")

    def test_same_operations_parameters_and_response_codes(self) -> None:
        for path, method, _ in OPERATIONS:
            with self.subTest(operation=f"{method.upper()} {path}"):
                bff, service = self.bff["paths"][path][method], self.service["paths"][path][method]
                names = {p["name"] for p in service.get("parameters", [])}
                self.assertEqual({p["name"] for p in bff.get("parameters", [])}, names)
                # El BFF agrega sus propios `504 upstream_timeout` (y `503` del transporte).
                self.assertLessEqual(set(service["responses"]), set(bff["responses"]))

    def test_success_schemas_are_identical(self) -> None:
        for path, method, status in OPERATIONS:
            with self.subTest(operation=f"{method.upper()} {path}"):
                self.assertEqual(
                    _success_schema(self.bff, path, method, status),
                    _success_schema(self.service, path, method, status),
                )

    def test_create_body_differs_only_in_the_athlete_identifier(self) -> None:
        bff, service = _request_schema(self.bff), _request_schema(self.service)

        self.assertIn("athlete_email", bff["properties"])
        self.assertIn("athlete_id", service["properties"])
        self.assertEqual(
            {k: v for k, v in bff["properties"].items() if k != "athlete_email"},
            {k: v for k, v in service["properties"].items() if k != "athlete_id"},
        )
        self.assertEqual(
            set(bff["required"]) - {"athlete_email"}, set(service["required"]) - {"athlete_id"}
        )
        self.assertIs(bff.get("additionalProperties"), False)
        self.assertEqual(bff.get("additionalProperties"), service.get("additionalProperties"))

    def test_catalog_publishes_etag_and_not_modified(self) -> None:
        for document in (self.bff, self.service):
            responses = document["paths"]["/v1/food-catalog"]["get"]["responses"]
            with self.subTest(title=document["info"]["title"]):
                self.assertIn("ETag", responses["200"]["headers"])
                self.assertIn("304", responses)
        # El BFF documenta además que el `304` conserva el `ETag` del servicio.
        bff_304 = self.bff["paths"]["/v1/food-catalog"]["get"]["responses"]["304"]
        self.assertIn("ETag", bff_304["headers"])


if __name__ == "__main__":
    unittest.main()
