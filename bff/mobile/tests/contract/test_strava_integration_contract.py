"""HU031 (ADR-042): `bff-mobile` expone la vinculación con Strava con el mismo contrato que
`third-party-management`.

Compara los OpenAPI versionados, operación por operación: respuesta de éxito idéntica (sin
títulos, descripciones ni ejemplos), ninguna operación autenticada con cuerpo y el BFF
documenta todos los estados del servicio (además de los propios del borde). El callback
público (`GET` con parámetros de consulta → `302`) se traduce al contrato interno
(`POST` con cuerpo JSON → `result`): mismos parámetros, misma longitud máxima y mismo catálogo
de resultados que el enlace profundo.
"""

import json
import unittest
from pathlib import Path
from typing import Any

from bff_mobile.api.strava_integration import CALLBACK_RESULTS
from sportapp_platform.testing.contracts import openapi_contract_path, repository_root

DOCUMENTATION_KEYS = {"title", "description", "examples"}
INTEGRATION = "/v1/me/integrations/strava"
CALLBACK = "/v1/integrations/strava/callback"
CALLBACK_PARAMETERS = {"code", "state", "scope", "error"}
MAX_LENGTH = 512  # ADR-042 / contrato interno del callback
# (método, ruta, estado de éxito, ¿tiene cuerpo de respuesta?)
OPERATIONS = (
    ("post", f"{INTEGRATION}/authorization", "201", True),
    ("get", INTEGRATION, "200", True),
    ("delete", INTEGRATION, "204", False),
    ("post", f"{INTEGRATION}/syncs", "202", True),
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


def _collect(node: Any, keys: tuple[str, ...]) -> set[Any]:
    """Valores de `keys` en todo el esquema (también dentro de `anyOf`, por el `null`)."""
    found: set[Any] = set()
    if isinstance(node, list):
        for item in node:
            found |= _collect(item, keys)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in keys:
                found |= set(value) if isinstance(value, list) else {value}
            else:
                found |= _collect(value, keys)
    return found


def _max_lengths(node: Any) -> set[Any]:
    return _collect(node, ("maxLength",))


def _enum_values(node: Any) -> set[Any]:
    return _collect(node, ("enum", "const"))


class StravaIntegrationContractTest(unittest.TestCase):
    service: dict[str, Any]
    bff: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.service = _load("third-party-management")
        cls.bff = _load("bff-mobile")

    def operations(self, method: str, path: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return self.bff["paths"][path][method], self.service["paths"][path][method]

    def test_success_responses_are_identical(self) -> None:
        for method, path, status, has_body in OPERATIONS:
            with self.subTest(method=method, path=path):
                bff, service = self.operations(method, path)
                if not has_body:
                    self.assertNotIn("content", bff["responses"][status])
                    self.assertNotIn("content", service["responses"][status])
                    continue
                self.assertEqual(
                    _json_schema(self.bff, bff["responses"][status]),
                    _json_schema(self.service, service["responses"][status]),
                )

    def test_authenticated_operations_have_no_request_body(self) -> None:
        for method, path, _, _ in OPERATIONS:
            with self.subTest(method=method, path=path):
                bff, service = self.operations(method, path)
                self.assertNotIn("requestBody", bff)
                self.assertNotIn("requestBody", service)

    def test_bff_documents_every_service_status(self) -> None:
        for method, path, _, _ in OPERATIONS:
            with self.subTest(method=method, path=path):
                bff, service = self.operations(method, path)
                self.assertLessEqual(set(service["responses"]), set(bff["responses"]))

    def test_callback_parameters_match_the_internal_body(self) -> None:
        """Mismos nombres y misma longitud máxima en la consulta pública y en el cuerpo interno."""
        bff = self.bff["paths"][CALLBACK]["get"]
        service = self.service["paths"][CALLBACK]["post"]
        body = _json_schema(self.service, service["requestBody"])
        query = {parameter["name"]: parameter for parameter in bff["parameters"]}

        self.assertEqual(set(query), CALLBACK_PARAMETERS)
        self.assertEqual(set(body["properties"]), CALLBACK_PARAMETERS)
        self.assertFalse(body.get("required"), "todos los parámetros son opcionales")
        self.assertEqual(body.get("additionalProperties"), False)
        for name in CALLBACK_PARAMETERS:
            with self.subTest(parameter=name):
                self.assertEqual(query[name]["in"], "query")
                self.assertFalse(query[name].get("required", False))
                self.assertEqual(_max_lengths(query[name]["schema"]), {MAX_LENGTH})
                self.assertEqual(
                    _max_lengths(query[name]["schema"]),
                    _max_lengths(body["properties"][name]),
                )

    def test_callback_results_match_the_deep_link(self) -> None:
        """El catálogo `result` del servicio es el del enlace profundo; el BFF redirige (`302`)."""
        service = self.service["paths"][CALLBACK]["post"]
        result = _json_schema(self.service, service["responses"]["200"])["properties"]["result"]
        self.assertEqual(_enum_values(result), set(CALLBACK_RESULTS))

        bff = self.bff["paths"][CALLBACK]["get"]
        self.assertNotIn("requestBody", bff)
        self.assertIn("Location", bff["responses"]["302"]["headers"])
        self.assertIn("Cache-Control", bff["responses"]["302"]["headers"])


if __name__ == "__main__":
    unittest.main()
