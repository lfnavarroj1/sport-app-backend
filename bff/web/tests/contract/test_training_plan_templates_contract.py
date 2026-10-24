"""HU028 (ADR-041): `bff-web` y `training-management` publican las tres operaciones de autoría
de plantillas con los mismos parámetros, cuerpos, códigos de respuesta y esquemas de éxito. La
consulta de sugerencias no pertenece al canal web."""

import unittest

from test_event_publication_contract import _document
from test_training_routes_contract import _request_schema, _success_schema

OPERATIONS = (
    ("/v1/training-plan-templates", "post", "201"),
    ("/v1/training-plan-templates/{template_id}/retire", "post", "200"),
    ("/v1/me/training-plan-templates", "get", "200"),
)


class TrainingPlanTemplatesContractTest(unittest.TestCase):
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
                self.assertLessEqual(set(service["responses"]), set(bff["responses"]))

    def test_success_and_request_schemas_are_identical(self) -> None:
        for path, method, status in OPERATIONS:
            with self.subTest(operation=f"{method.upper()} {path}"):
                self.assertEqual(
                    _success_schema(self.bff, path, method, status),
                    _success_schema(self.service, path, method, status),
                )
        for path in (OPERATIONS[0][0], OPERATIONS[1][0]):
            with self.subTest(body=path):
                bff = _request_schema(self.bff, path)
                self.assertEqual(bff, _request_schema(self.service, path))
                self.assertIs(bff.get("additionalProperties"), False)

    def test_suggestions_are_not_published_on_the_web_channel(self) -> None:
        path = "/v1/me/suggestions/training-plan-templates"
        self.assertIn(path, self.service["paths"])
        self.assertNotIn(path, self.bff["paths"])


if __name__ == "__main__":
    unittest.main()
