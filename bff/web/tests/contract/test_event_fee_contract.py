"""HU025 (ADR-040): `bff-web` y `events-management` publican `PUT` y `DELETE` de la tarifa con
los mismos esquemas de cuerpo y respuesta, parámetros y códigos; las consultas exponen la
misma tarifa y `registration_count`.

Compara los OpenAPI versionados: el BFF reenvía cuerpo, parámetro, clave y respuestas del
servicio sin reinterpretarlos, así que una diferencia sería una API paralela.
"""

import unittest

from test_event_publication_contract import _document, _ok_schema, _resolve

FEE_PATH = "/v1/events/{event_id}/fee"
OPERATIONS = ((FEE_PATH, "put"), (FEE_PATH, "delete"))


class EventFeeContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bff = _document("bff-web")
        self.service = _document("events-management")

    def test_same_operations_parameters_and_error_codes(self) -> None:
        for path, method in OPERATIONS:
            with self.subTest(operation=f"{method.upper()} {path}"):
                bff, service = self.bff["paths"][path][method], self.service["paths"][path][method]
                names = {p["name"] for p in service.get("parameters", [])}
                self.assertEqual({p["name"] for p in bff.get("parameters", [])}, names)
                self.assertIn("Idempotency-Key", names)
                expected = {"200", "401", "403", "404", "409", "422", "503"}
                self.assertLessEqual(expected, set(service["responses"]))
                # El BFF agrega su propio `504 upstream_timeout`.
                self.assertLessEqual(set(service["responses"]), set(bff["responses"]))
        delete = {p["name"]: p for p in self.bff["paths"][FEE_PATH]["delete"]["parameters"]}
        self.assertEqual(delete["expected_version"]["in"], "query")

    def test_same_request_body(self) -> None:
        def body(document: dict[str, object]) -> dict[str, object]:
            operation = document["paths"][FEE_PATH]["put"]  # type: ignore[index]
            schema = operation["requestBody"]["content"]["application/json"]["schema"]
            return _resolve(document, schema)  # type: ignore[arg-type]

        service = body(self.service)
        self.assertEqual(body(self.bff), service)
        self.assertEqual(set(service["required"]), {"amount", "currency", "expected_version"})  # type: ignore[arg-type]
        self.assertFalse(service["additionalProperties"])

    def test_same_response_with_fee_and_registration_count(self) -> None:
        for path, method in (*OPERATIONS, ("/v1/me/events", "get")):
            with self.subTest(operation=f"{method.upper()} {path}"):
                service = _ok_schema(self.service, path, method)
                self.assertEqual(_ok_schema(self.bff, path, method), service)
        response = _ok_schema(self.service, FEE_PATH, "put")
        self.assertIn("registration_count", response["required"])
        fee = response["properties"]["fee"]["anyOf"][0]
        self.assertEqual(set(fee["properties"]), {"amount", "currency"})

    def test_public_queries_expose_the_fee(self) -> None:
        detail = _ok_schema(self.bff, "/v1/events/{event_id}", "get")
        listing = _ok_schema(self.bff, "/v1/events", "get")
        item = listing["properties"]["items"]["items"]

        for schema in (detail, item):
            fee = schema["properties"]["fee"]["anyOf"][0]
            self.assertEqual(set(fee["properties"]), {"amount", "currency"})
        self.assertIn("registration_count", detail["properties"])
        self.assertEqual(
            item, _ok_schema(self.service, "/v1/events", "get")["properties"]["items"]["items"]
        )


if __name__ == "__main__":
    unittest.main()
