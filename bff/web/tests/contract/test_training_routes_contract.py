"""HU026 (ADR-041): `bff-web` y `training-management` publican las tres operaciones de autoría
de rutas con los mismos parámetros, cuerpos, códigos de respuesta y esquemas de éxito.

El BFF reenvía cuerpo y respuestas sin reinterpretarlos: una diferencia sería una API paralela.
La consulta de sugerencias (`GET /v1/me/suggestions/routes`) no pertenece al canal web.
"""

import unittest
from typing import Any

from test_event_publication_contract import _document, _resolve

OPERATIONS = (
    ("/v1/routes", "post", "201"),
    ("/v1/routes/{route_id}/retire", "post", "200"),
    ("/v1/me/routes", "get", "200"),
)


def _success_schema(
    document: dict[str, Any], path: str, method: str, status: str
) -> dict[str, Any]:
    content = document["paths"][path][method]["responses"][status]["content"]
    return _resolve(document, content["application/json"]["schema"])


def _request_schema(document: dict[str, Any], path: str) -> dict[str, Any]:
    content = document["paths"][path]["post"]["requestBody"]["content"]
    return _resolve(document, content["application/json"]["schema"])


class TrainingRoutesContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bff = _document("bff-web")
        self.service = _document("training-management")

    def test_same_operations_parameters_and_response_codes(self) -> None:
        for path, method, _ in OPERATIONS:
            with self.subTest(operation=f"{method.upper()} {path}"):
                bff, service = self.bff["paths"][path][method], self.service["paths"][path][method]
                self.assertEqual(
                    {p["name"] for p in bff.get("parameters", [])},
                    {p["name"] for p in service.get("parameters", [])},
                )
                # El BFF agrega sus propios `503`/`504` del transporte.
                self.assertLessEqual(set(service["responses"]), set(bff["responses"]))

    def test_success_and_request_schemas_are_identical(self) -> None:
        for path, method, status in OPERATIONS:
            with self.subTest(operation=f"{method.upper()} {path}"):
                self.assertEqual(
                    _success_schema(self.bff, path, method, status),
                    _success_schema(self.service, path, method, status),
                )
        for path in ("/v1/routes", "/v1/routes/{route_id}/retire"):
            with self.subTest(body=path):
                bff, service = _request_schema(self.bff, path), _request_schema(self.service, path)
                self.assertEqual(bff, service)
                self.assertIs(bff.get("additionalProperties"), False)

    def test_suggestions_are_not_published_on_the_web_channel(self) -> None:
        self.assertIn("/v1/me/suggestions/routes", self.service["paths"])
        self.assertNotIn("/v1/me/suggestions/routes", self.bff["paths"])


if __name__ == "__main__":
    unittest.main()
