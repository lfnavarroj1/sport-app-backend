"""HU024 (ADR-033): `bff-web` y `events-management` publican las tres operaciones con los mismos
esquemas, parámetros y códigos de respuesta; el detalle web solo omite `my_registration`.

Compara los OpenAPI versionados: el BFF reenvía las respuestas del servicio sin
reinterpretarlas, así que una diferencia sería una API paralela.
"""

import json
import unittest
from pathlib import Path
from typing import Any

from sportapp_platform.testing.contracts import openapi_contract_path, repository_root

OPERATIONS = (
    ("/v1/events/{event_id}/publish", "post"),
    ("/v1/me/events", "get"),
    ("/v1/events/{event_id}", "get"),
)


def _document(component: str) -> dict[str, Any]:
    path = openapi_contract_path(repository_root(Path(__file__).resolve()), component)
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def _resolve(document: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Esquema con las referencias internas resueltas, para comparar estructuras."""
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return _resolve(document, document["components"]["schemas"][name])
    resolved: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "title":
            continue  # el nombre del modelo puede diferir entre componentes
        if isinstance(value, dict):
            resolved[key] = _resolve(document, value)
        elif isinstance(value, list):
            resolved[key] = [_resolve(document, v) if isinstance(v, dict) else v for v in value]
        else:
            resolved[key] = value
    return resolved


def _ok_schema(document: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    content = document["paths"][path][method]["responses"]["200"]["content"]
    return _resolve(document, content["application/json"]["schema"])


class EventPublicationContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bff = _document("bff-web")
        self.service = _document("events-management")

    def test_same_operations_parameters_and_error_codes(self) -> None:
        for path, method in OPERATIONS:
            with self.subTest(operation=f"{method.upper()} {path}"):
                bff, service = self.bff["paths"][path][method], self.service["paths"][path][method]
                names = {p["name"] for p in service.get("parameters", [])}
                self.assertEqual({p["name"] for p in bff.get("parameters", [])}, names)
                # El BFF agrega sus propios `504 upstream_timeout` (y `503` del transporte).
                self.assertLessEqual(set(service["responses"]), set(bff["responses"]))

    def test_publish_and_list_return_the_same_aggregate(self) -> None:
        for path, method in OPERATIONS[:2]:
            with self.subTest(operation=f"{method.upper()} {path}"):
                self.assertEqual(
                    _ok_schema(self.bff, path, method), _ok_schema(self.service, path, method)
                )

    def test_web_detail_is_the_service_detail_without_my_registration(self) -> None:
        path, method = OPERATIONS[2]
        service = _ok_schema(self.service, path, method)
        bff = _ok_schema(self.bff, path, method)

        self.assertIn("my_registration", service["properties"])
        expected = {k: v for k, v in service["properties"].items() if k != "my_registration"}
        self.assertEqual(bff["properties"], expected)
        self.assertEqual(set(bff["required"]), set(service["required"]) - {"my_registration"})
        self.assertEqual(bff["properties"]["status"]["enum"], ["DRAFT", "PUBLISHED"])


if __name__ == "__main__":
    unittest.main()
