"""HU012 (ADR-037): `bff-web` y `training-management` publican las dos consultas del historial
de salud con los mismos parámetros, códigos de respuesta y esquema de éxito.

Compara los OpenAPI versionados: el BFF reenvía las respuestas del servicio sin
reinterpretarlas, así que una diferencia sería una API paralela.
"""

import unittest

from test_event_publication_contract import _document, _ok_schema, _resolve

OPERATIONS = ("/v1/me/health-reports", "/v1/athletes/{athlete_id}/health-reports")
# ADR-037: lo único que recibe el profesional (y el deportista) de cada reporte.
ITEM_FIELDS = {
    "report_id",
    "type",
    "body_area",
    "severity",
    "occurred_at",
    "expected_recovery_days",
    "status",
    "session_id",
}
PARAMETERS = {"from", "to", "type", "severity", "limit", "cursor"}


class HealthHistoryContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bff = _document("bff-web")
        self.service = _document("training-management")

    def test_same_operations_parameters_and_response_codes(self) -> None:
        for path in OPERATIONS:
            with self.subTest(path=path):
                bff, service = self.bff["paths"][path]["get"], self.service["paths"][path]["get"]
                names = {p["name"] for p in service["parameters"]}
                self.assertEqual({p["name"] for p in bff["parameters"]}, names)
                self.assertLessEqual(PARAMETERS, names)
                # El BFF agrega sus propios `504 upstream_timeout` (y `503` del transporte).
                self.assertLessEqual(set(service["responses"]), set(bff["responses"]))
                self.assertLessEqual({"200", "401", "403", "422"}, set(service["responses"]))

    def test_filter_catalogs_are_identical(self) -> None:
        for path in OPERATIONS:
            for name in ("type", "severity"):
                with self.subTest(path=path, parameter=name):
                    [bff] = [
                        p for p in self.bff["paths"][path]["get"]["parameters"] if p["name"] == name
                    ]
                    [service] = [
                        p
                        for p in self.service["paths"][path]["get"]["parameters"]
                        if p["name"] == name
                    ]
                    self.assertEqual(
                        _resolve(self.bff, bff["schema"]), _resolve(self.service, service["schema"])
                    )

    def test_success_schemas_are_identical_and_limited_to_adr_037(self) -> None:
        for path in OPERATIONS:
            with self.subTest(path=path):
                bff = _ok_schema(self.bff, path, "get")
                self.assertEqual(bff, _ok_schema(self.service, path, "get"))
                self.assertEqual(set(bff["properties"]), {"items", "next_cursor"})
                item = bff["properties"]["items"]["items"]
                self.assertEqual(set(item["properties"]), ITEM_FIELDS)


if __name__ == "__main__":
    unittest.main()
