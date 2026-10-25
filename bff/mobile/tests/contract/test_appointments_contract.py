"""HU040 (ADR-046): `bff-mobile` expone las citas virtuales y la disponibilidad (ADR-038) con
el mismo contrato que `complementary-services`.

Compara los OpenAPI versionados, operación por operación: respuesta de éxito y cuerpo de
solicitud idénticos (sin títulos, descripciones ni ejemplos), los mismos parámetros (el BFF
los reenvía tal como llegan), las cabeceras del `201` y que el BFF documente todos los estados
del servicio además de su `504`.
"""

import unittest
from typing import Any

from test_session_reminders_contract import _json_schema, _load, _resolved

APPOINTMENTS = "/v1/me/appointments"
APPOINTMENT = "/v1/me/appointments/{appointment_id}"
AVAILABILITY = "/v1/services/availability"
# (método, ruta, estado de éxito, ¿tiene cuerpo?)
OPERATIONS = (
    ("post", APPOINTMENTS, "201", True),
    ("delete", APPOINTMENT, "200", False),
    ("get", APPOINTMENTS, "200", False),
    ("get", AVAILABILITY, "200", False),
)


def _parameters(operation: dict[str, Any]) -> dict[tuple[str, str], Any]:
    return {
        (parameter["in"], parameter["name"]): parameter.get("required", False)
        for parameter in operation.get("parameters", [])
    }


class AppointmentsContractTest(unittest.TestCase):
    service: dict[str, Any]
    bff: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.service = _load("complementary-services")
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

    def test_parameters_are_the_same_as_the_service(self) -> None:
        for method, path, _, _ in OPERATIONS:
            with self.subTest(method=method, path=path):
                bff, service = self.operations(method, path)
                self.assertEqual(_parameters(bff), _parameters(service))
        bff, service = self.operations("delete", APPOINTMENT)
        path_schema = [
            _resolved(document, parameter["schema"])
            for document, operation in ((self.bff, bff), (self.service, service))
            for parameter in operation["parameters"]
            if parameter["in"] == "path"
        ]
        self.assertEqual(path_schema[0], path_schema[1])
        self.assertEqual(path_schema[0]["format"], "uuid")

    def test_repeatable_filters_of_availability_are_arrays(self) -> None:
        bff, _ = self.operations("get", AVAILABILITY)
        for name in ("category", "modality"):
            schema = next(p["schema"] for p in bff["parameters"] if p["name"] == name)
            with self.subTest(parameter=name):
                self.assertIn({"type": "array", "items": {"type": "string"}}, schema["anyOf"])

    def test_booking_documents_location_and_replay_headers(self) -> None:
        bff, service = self.operations("post", APPOINTMENTS)
        for operation in (bff, service):
            self.assertLessEqual(
                {"Location", "Idempotent-Replayed"}, set(operation["responses"]["201"]["headers"])
            )

    def test_bff_documents_every_service_status_and_its_timeout(self) -> None:
        for method, path, _, _ in OPERATIONS:
            with self.subTest(method=method, path=path):
                bff, service = self.operations(method, path)
                self.assertLessEqual(set(service["responses"]), set(bff["responses"]))
                self.assertIn("504", bff["responses"])

    def test_interval_carries_its_availability_id_and_the_appointment_has_no_meeting(
        self,
    ) -> None:
        bff, _ = self.operations("get", AVAILABILITY)
        page = _json_schema(self.bff, bff["responses"]["200"])
        interval = page["properties"]["items"]["items"]["properties"]["availability"]["items"]
        self.assertIn("availability_id", interval["required"])
        self.assertIn("restricted_categories", page["required"])
        booked, _ = self.operations("post", APPOINTMENTS)
        appointment = _json_schema(self.bff, booked["responses"]["201"])
        self.assertEqual(appointment["properties"]["meeting_url"], {"type": "null"})
        self.assertEqual(
            appointment["properties"]["status"]["enum"], ["BOOKED", "CANCELLED_BY_ATHLETE"]
        )


if __name__ == "__main__":
    unittest.main()
