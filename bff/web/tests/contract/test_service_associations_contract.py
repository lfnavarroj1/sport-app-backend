"""HU017 (ADR-038): `bff-web` expone las tres operaciones de asociación con el mismo
contrato que `complementary-services`.

Compara los OpenAPI versionados: cuerpo de la solicitud, parámetros y respuestas de éxito
idénticos (sin títulos ni descripciones) y los mismos errores del servicio; el BFF solo
agrega el `504` de sus propias llamadas.
"""

import unittest
from typing import Any

from test_profile_contract import _json_schema, _load

PATH = "/v1/me/service-associations"
ITEM = "/v1/me/service-associations/{association_id}"
OPERATIONS = ((PATH, "post", "201"), (PATH, "get", "200"), (ITEM, "delete", "204"))


class ServiceAssociationsContractEquivalenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = _load("complementary-services")
        cls.bff = _load("bff-web")

    def operation(self, document: dict[str, Any], path: str, method: str) -> dict[str, Any]:
        operation: dict[str, Any] = document["paths"][path][method]
        return operation

    def test_request_body_and_parameters_are_identical(self) -> None:
        service = self.operation(self.service, PATH, "post")
        bff = self.operation(self.bff, PATH, "post")

        self.assertEqual(
            _json_schema(self.bff, bff["requestBody"]),
            _json_schema(self.service, service["requestBody"]),
        )
        for path, method, _ in OPERATIONS:
            with self.subTest(operation=f"{method.upper()} {path}"):
                self.assertEqual(
                    {p["name"] for p in self.operation(self.bff, path, method)["parameters"]},
                    {p["name"] for p in self.operation(self.service, path, method)["parameters"]},
                )

    def test_success_responses_are_identical(self) -> None:
        for path, method, status in OPERATIONS:
            with self.subTest(operation=f"{method.upper()} {path}"):
                service = self.operation(self.service, path, method)["responses"][status]
                bff = self.operation(self.bff, path, method)["responses"][status]
                if "content" in service:
                    self.assertEqual(
                        _json_schema(self.bff, bff), _json_schema(self.service, service)
                    )
                else:
                    self.assertNotIn("content", bff)
                self.assertEqual(set(bff.get("headers", {})), set(service.get("headers", {})))

    def test_bff_documents_every_service_status_plus_its_own_upstream_errors(self) -> None:
        for path, method, _ in OPERATIONS:
            with self.subTest(operation=f"{method.upper()} {path}"):
                service = set(self.operation(self.service, path, method)["responses"])
                bff = set(self.operation(self.bff, path, method)["responses"])
                self.assertLessEqual(service, bff)
                self.assertLessEqual(bff - service, {"504"})


if __name__ == "__main__":
    unittest.main()
