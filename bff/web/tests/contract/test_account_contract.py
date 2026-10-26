"""HU002 (ADR-035): `bff-web` expone `PUT` y `GET /v1/me/account` con el mismo contrato que
`users-management`.

Compara los OpenAPI versionados: cuerpo de la solicitud y respuestas de éxito idénticos (sin
títulos ni descripciones) y los mismos errores del servicio; el BFF solo agrega los
`503`/`504` de sus propias llamadas. No hay `403`: los tres actores editan su cuenta.
"""

import unittest
from typing import Any

from test_profile_contract import _json_schema, _load

PATH = "/v1/me/account"


class AccountContractEquivalenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = _load("users-management")
        cls.bff = _load("bff-web")

    def operation(self, document: dict[str, Any], method: str) -> dict[str, Any]:
        operation: dict[str, Any] = document["paths"][PATH][method]
        return operation

    def test_request_bodies_are_identical(self) -> None:
        service = self.operation(self.service, "put")["requestBody"]
        bff = self.operation(self.bff, "put")["requestBody"]

        self.assertEqual(_json_schema(self.bff, bff), _json_schema(self.service, service))
        self.assertEqual(
            set(_json_schema(self.bff, bff)["required"]), {"expected_version", "full_name", "email"}
        )

    def test_success_responses_are_identical(self) -> None:
        for method in ("put", "get"):
            with self.subTest(method=method):
                service = self.operation(self.service, method)["responses"]["200"]
                bff = self.operation(self.bff, method)["responses"]["200"]

                self.assertEqual(_json_schema(self.bff, bff), _json_schema(self.service, service))

    def test_bff_documents_every_service_status_plus_its_own_upstream_errors(self) -> None:
        for method in ("put", "get"):
            with self.subTest(method=method):
                service = set(self.operation(self.service, method)["responses"])
                bff = set(self.operation(self.bff, method)["responses"])

                self.assertLessEqual(service, bff)
                self.assertLessEqual(bff - service, {"503", "504"})
                self.assertNotIn("403", bff)


if __name__ == "__main__":
    unittest.main()
