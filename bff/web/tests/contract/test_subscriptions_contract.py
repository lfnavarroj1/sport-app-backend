"""HU016 (ADR-036): `bff-web` expone las tres operaciones de afiliación con el mismo contrato
que `affiliation-management`.

Compara los OpenAPI versionados: cuerpo de la solicitud y respuestas de éxito idénticos (sin
títulos ni descripciones) y los mismos errores del servicio; el BFF solo agrega los
`503`/`504` de sus propias llamadas.
"""

import unittest
from typing import Any

from test_profile_contract import _json_schema, _load

OPERATIONS = (
    ("/v1/plans", "get"),
    ("/v1/me/subscription", "get"),
    ("/v1/me/subscription/changes", "post"),
)


class SubscriptionsContractEquivalenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = _load("affiliation-management")
        cls.bff = _load("bff-web")

    def operation(self, document: dict[str, Any], path: str, method: str) -> dict[str, Any]:
        operation: dict[str, Any] = document["paths"][path][method]
        return operation

    def test_request_body_and_parameters_are_identical(self) -> None:
        path, method = OPERATIONS[2]
        service, bff = (
            self.operation(self.service, path, method),
            self.operation(self.bff, path, method),
        )

        self.assertEqual(
            _json_schema(self.bff, bff["requestBody"]),
            _json_schema(self.service, service["requestBody"]),
        )
        self.assertEqual(
            {p["name"] for p in bff["parameters"]}, {p["name"] for p in service["parameters"]}
        )

    def test_success_responses_are_identical(self) -> None:
        for path, method in OPERATIONS:
            with self.subTest(operation=f"{method.upper()} {path}"):
                service = self.operation(self.service, path, method)["responses"]["200"]
                bff = self.operation(self.bff, path, method)["responses"]["200"]
                self.assertEqual(_json_schema(self.bff, bff), _json_schema(self.service, service))

    def test_bff_documents_every_service_status_plus_its_own_upstream_errors(self) -> None:
        for path, method in OPERATIONS:
            with self.subTest(operation=f"{method.upper()} {path}"):
                service = set(self.operation(self.service, path, method)["responses"])
                bff = set(self.operation(self.bff, path, method)["responses"])
                self.assertLessEqual(service - {"503"}, bff)
                self.assertLessEqual(bff - service, {"503", "504"})


if __name__ == "__main__":
    unittest.main()
