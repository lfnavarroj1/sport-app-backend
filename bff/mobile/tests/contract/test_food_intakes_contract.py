"""HU043 (ADR-037): `bff-mobile` expone el catálogo de alimentos y el registro de consumos con
el mismo contrato que `training-management`.

Compara los OpenAPI versionados, operación por operación: respuesta de éxito y cuerpo de
solicitud idénticos (sin títulos, descripciones ni ejemplos) y el BFF documenta todos los
estados del servicio (además de los propios del borde).
"""

import json
import unittest
from pathlib import Path
from typing import Any

from sportapp_platform.testing.contracts import openapi_contract_path, repository_root

DOCUMENTATION_KEYS = {"title", "description", "examples"}
# (método, ruta, estado de éxito, ¿tiene cuerpo?)
OPERATIONS = (
    ("get", "/v1/food-catalog", "200", False),
    ("post", "/v1/me/food-intakes", "201", True),
)


def _load(component: str) -> dict[str, Any]:
    path = openapi_contract_path(repository_root(Path(__file__).resolve()), component)
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def _resolved(document: dict[str, Any], node: Any) -> Any:
    """Esquema con las referencias resueltas y sin claves de documentación."""
    if isinstance(node, list):
        return [_resolved(document, item) for item in node]
    if not isinstance(node, dict):
        return node
    reference = node.get("$ref")
    if isinstance(reference, str):
        name = reference.rsplit("/", 1)[-1]
        return _resolved(document, document["components"]["schemas"][name])
    return {
        key: _resolved(document, value)
        for key, value in node.items()
        if key not in DOCUMENTATION_KEYS
    }


def _json_schema(document: dict[str, Any], content: dict[str, Any]) -> Any:
    return _resolved(document, content["content"]["application/json"]["schema"])


class FoodIntakesContractTest(unittest.TestCase):
    service: dict[str, Any]
    bff: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.service = _load("training-management")
        cls.bff = _load("bff-mobile")

    def operations(self, method: str, path: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return self.bff["paths"][path][method], self.service["paths"][path][method]

    def test_success_responses_are_identical(self) -> None:
        for method, path, status, _ in OPERATIONS:
            with self.subTest(method=method, path=path):
                bff, service = self.operations(method, path)
                self.assertEqual(
                    _json_schema(self.bff, bff["responses"][status]),
                    _json_schema(self.service, service["responses"][status]),
                )

    def test_request_bodies_are_identical(self) -> None:
        for method, path, _, has_body in OPERATIONS:
            with self.subTest(method=method, path=path):
                bff, service = self.operations(method, path)
                if not has_body:
                    self.assertNotIn("requestBody", bff)
                    self.assertNotIn("requestBody", service)
                    continue
                self.assertEqual(
                    _json_schema(self.bff, bff["requestBody"]),
                    _json_schema(self.service, service["requestBody"]),
                )

    def test_bff_documents_every_service_status(self) -> None:
        for method, path, _, _ in OPERATIONS:
            with self.subTest(method=method, path=path):
                bff, service = self.operations(method, path)
                self.assertLessEqual(set(service["responses"]), set(bff["responses"]))

    def test_catalog_documents_etag_and_not_modified(self) -> None:
        bff, service = self.operations("get", "/v1/food-catalog")
        for operation in (bff, service):
            self.assertIn("304", operation["responses"])
            self.assertIn("ETag", operation["responses"]["200"]["headers"])
        self.assertIn("If-None-Match", {item["name"] for item in bff.get("parameters", [])})

    def test_intake_documents_idempotency_header(self) -> None:
        bff, service = self.operations("post", "/v1/me/food-intakes")
        for operation in (bff, service):
            self.assertIn(
                "Idempotency-Key", {item["name"] for item in operation.get("parameters", [])}
            )
            self.assertIn("Idempotent-Replayed", operation["responses"]["201"]["headers"])


if __name__ == "__main__":
    unittest.main()
