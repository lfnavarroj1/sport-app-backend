"""HU039 (ADR-045 §11): `bff-mobile` expone la bandeja, las preferencias y el token push con
el mismo contrato que `events-management`.

Compara los OpenAPI versionados, operación por operación: respuesta de éxito y cuerpo de
solicitud idénticos (sin títulos, descripciones ni ejemplos), los mismos parámetros de consulta
y el BFF documenta todos los estados del servicio (además de los propios del borde).
"""

import json
import unittest
from pathlib import Path
from typing import Any

from sportapp_platform.testing.contracts import openapi_contract_path, repository_root

DOCUMENTATION_KEYS = {"title", "description", "examples"}
# (método, ruta, estado de éxito, ¿tiene cuerpo?)
OPERATIONS = (
    ("get", "/v1/me/notifications", "200", False),
    ("post", "/v1/me/notifications/{notification_id}/read", "200", False),
    ("get", "/v1/me/notification-preferences", "200", False),
    ("put", "/v1/me/notification-preferences", "200", True),
    ("put", "/v1/me/push-tokens", "204", True),
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


def _query_parameters(operation: dict[str, Any]) -> set[str]:
    return {item["name"] for item in operation.get("parameters", []) if item["in"] == "query"}


class NotificationsContractTest(unittest.TestCase):
    service: dict[str, Any]
    bff: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.service = _load("events-management")
        cls.bff = _load("bff-mobile")

    def operations(self, method: str, path: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return self.bff["paths"][path][method], self.service["paths"][path][method]

    def test_success_responses_are_identical(self) -> None:
        for method, path, status, _ in OPERATIONS:
            with self.subTest(method=method, path=path):
                bff, service = self.operations(method, path)
                if status == "204":
                    self.assertNotIn("content", bff["responses"][status])
                    self.assertNotIn("content", service["responses"][status])
                    continue
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

    def test_query_parameters_are_the_same(self) -> None:
        for method, path, _, _ in OPERATIONS:
            with self.subTest(method=method, path=path):
                bff, service = self.operations(method, path)
                self.assertEqual(_query_parameters(bff), _query_parameters(service))

    def test_bff_documents_every_service_status(self) -> None:
        for method, path, _, _ in OPERATIONS:
            with self.subTest(method=method, path=path):
                bff, service = self.operations(method, path)
                self.assertLessEqual(set(service["responses"]), set(bff["responses"]))


if __name__ == "__main__":
    unittest.main()
