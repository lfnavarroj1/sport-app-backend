"""HU033 (ADR-043): `bff-mobile` expone `GET /v1/me/training-dashboard` con el mismo contrato
que `training-management`.

Compara los OpenAPI versionados: parámetros y respuesta de éxito idénticos (sin títulos,
descripciones ni ejemplos) y los mismos errores del servicio.
"""

import json
import unittest
from pathlib import Path
from typing import Any

from sportapp_platform.testing.contracts import openapi_contract_path, repository_root

PATH, METHOD = "/v1/me/training-dashboard", "get"
DOCUMENTATION_KEYS = {"title", "description", "examples"}


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


class TrainingDashboardContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = _load("training-management")
        cls.bff = _load("bff-mobile")
        cls.service_operation = cls.service["paths"][PATH][METHOD]
        cls.bff_operation = cls.bff["paths"][PATH][METHOD]

    def test_success_response_is_identical(self) -> None:
        def schema(document: dict[str, Any], operation: dict[str, Any]) -> Any:
            return _resolved(
                document, operation["responses"]["200"]["content"]["application/json"]["schema"]
            )

        self.assertEqual(
            schema(self.bff, self.bff_operation), schema(self.service, self.service_operation)
        )

    def test_query_parameters_are_identical(self) -> None:
        def parameters(document: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
            return {
                item["name"]: _resolved(document, item["schema"])
                for item in operation["parameters"]
                if item["in"] == "query"
            }

        bff, service = (
            parameters(self.bff, self.bff_operation),
            parameters(self.service, self.service_operation),
        )
        self.assertEqual(set(bff), {"from", "to"})
        self.assertEqual(set(bff), set(service))

    def test_bff_documents_every_service_status(self) -> None:
        self.assertEqual(
            set(self.bff_operation["responses"]), set(self.service_operation["responses"])
        )


if __name__ == "__main__":
    unittest.main()
