"""HU042 (ADR-037): `bff-mobile` expone `POST /v1/me/health-reports` con el mismo contrato que
`training-management`.

Compara los OpenAPI versionados: respuesta de éxito y cuerpo de solicitud idénticos (sin
títulos, descripciones ni ejemplos), la cabecera `Idempotent-Replayed` del `201` y que el BFF
documente todos los estados del servicio además de su `504`.
"""

import unittest
from typing import Any

from test_session_reminders_contract import _json_schema, _load

PATH, METHOD = "/v1/me/health-reports", "post"


class HealthReportsContractTest(unittest.TestCase):
    service: dict[str, Any]
    bff: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.service = _load("training-management")
        cls.bff = _load("bff-mobile")

    def operations(self) -> tuple[dict[str, Any], dict[str, Any]]:
        return self.bff["paths"][PATH][METHOD], self.service["paths"][PATH][METHOD]

    def test_success_response_is_identical(self) -> None:
        bff, service = self.operations()
        self.assertEqual(
            _json_schema(self.bff, bff["responses"]["201"]),
            _json_schema(self.service, service["responses"]["201"]),
        )

    def test_request_body_is_identical(self) -> None:
        bff, service = self.operations()
        self.assertEqual(
            _json_schema(self.bff, bff["requestBody"]),
            _json_schema(self.service, service["requestBody"]),
        )

    def test_replay_header_is_documented(self) -> None:
        bff, service = self.operations()
        for operation in (bff, service):
            self.assertIn("Idempotent-Replayed", operation["responses"]["201"]["headers"])

    def test_bff_documents_every_service_status(self) -> None:
        bff, service = self.operations()
        self.assertLessEqual(set(service["responses"]), set(bff["responses"]))
        self.assertIn("504", bff["responses"])

    def test_idempotency_key_header_is_forwarded(self) -> None:
        bff, service = self.operations()
        for operation in (bff, service):
            names = {item["name"] for item in operation.get("parameters", [])}
            self.assertIn("Idempotency-Key", names)


if __name__ == "__main__":
    unittest.main()
