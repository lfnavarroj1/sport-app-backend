"""HU005 (ADR-034): `bff-web` expone las tres operaciones del perfil con el mismo contrato
que `users-management`.

Compara los OpenAPI versionados: cuerpo de las solicitudes y respuestas de éxito idénticos
(sin títulos ni descripciones) y los mismos errores del servicio; el BFF solo agrega los
`503`/`504` de sus propias llamadas.
"""

import json
import unittest
from pathlib import Path
from typing import Any

from sportapp_platform.testing.contracts import openapi_contract_path, repository_root

OPERATIONS = (
    ("/v1/me/demographic-profile", "put"),
    ("/v1/me/physiological-profile", "put"),
    ("/v1/me/profile", "get"),
)
DOCUMENTATION_KEYS = {"title", "description", "examples"}


def _load(component: str) -> dict[str, Any]:
    path = openapi_contract_path(repository_root(Path(__file__).resolve()), component)
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def _resolved(document: dict[str, Any], schema: Any) -> Any:
    """Esquema sin `$ref` ni textos de documentación, para comparar la forma."""
    if isinstance(schema, dict):
        if "$ref" in schema:
            name = schema["$ref"].rsplit("/", 1)[-1]
            return _resolved(document, document["components"]["schemas"][name])
        return {
            key: _resolved(document, value)
            for key, value in schema.items()
            if key not in DOCUMENTATION_KEYS
        }
    if isinstance(schema, list):
        return [_resolved(document, item) for item in schema]
    return schema


def _json_schema(document: dict[str, Any], container: dict[str, Any]) -> Any:
    return _resolved(document, container["content"]["application/json"]["schema"])


class ProfileContractEquivalenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = _load("users-management")
        cls.bff = _load("bff-web")

    def operation(self, document: dict[str, Any], path: str, method: str) -> dict[str, Any]:
        operation: dict[str, Any] = document["paths"][path][method]
        return operation

    def test_request_bodies_are_identical(self) -> None:
        for path, method in OPERATIONS[:2]:
            with self.subTest(path=path):
                service = self.operation(self.service, path, method)["requestBody"]
                bff = self.operation(self.bff, path, method)["requestBody"]
                self.assertEqual(_json_schema(self.bff, bff), _json_schema(self.service, service))

    def test_success_responses_are_identical(self) -> None:
        for path, method in OPERATIONS:
            service = self.operation(self.service, path, method)["responses"]
            bff = self.operation(self.bff, path, method)["responses"]
            for status in ("200", "201"):
                if status not in service:
                    continue
                with self.subTest(path=path, status=status):
                    self.assertEqual(
                        _json_schema(self.bff, bff[status]),
                        _json_schema(self.service, service[status]),
                    )

    def test_bff_documents_every_service_status_plus_its_own_upstream_errors(self) -> None:
        for path, method in OPERATIONS:
            with self.subTest(path=path):
                service = set(self.operation(self.service, path, method)["responses"])
                bff = set(self.operation(self.bff, path, method)["responses"])
                self.assertLessEqual(service, bff)
                self.assertLessEqual(bff - service, {"503", "504"})


if __name__ == "__main__":
    unittest.main()
