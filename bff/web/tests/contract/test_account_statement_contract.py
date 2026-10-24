"""HU007 (ADR-036): `bff-web` expone `GET /v1/me/account-statement` con el mismo contrato que
`affiliation-management`.

Compara los OpenAPI versionados: mismos parámetros (`limit` y `cursor`, sin ningún identificador
de usuario), respuesta `200` idéntica (sin títulos ni descripciones) y los mismos errores del
servicio; el BFF solo agrega los `503`/`504` de su propia llamada. Fija además las reglas de
ADR-036 que el cliente web consume: los cuatro `statement_status`, `payments_status` y `balance`.
"""

import unittest
from typing import Any

from test_profile_contract import _json_schema, _load, _resolved

PATH, METHOD = "/v1/me/account-statement", "get"


class AccountStatementContractEquivalenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = _load("affiliation-management")
        cls.bff = _load("bff-web")

    def operation(self, document: dict[str, Any]) -> dict[str, Any]:
        operation: dict[str, Any] = document["paths"][PATH][METHOD]
        return operation

    def test_query_parameters_are_identical_and_never_select_a_user(self) -> None:
        for document in (self.service, self.bff):
            names = {
                p["name"] for p in self.operation(document)["parameters"] if p["in"] == "query"
            }
            self.assertEqual(names, {"limit", "cursor"})
            self.assertNotIn("{", PATH)

    def test_success_response_is_identical(self) -> None:
        service = self.operation(self.service)["responses"]["200"]
        bff = self.operation(self.bff)["responses"]["200"]
        self.assertEqual(_json_schema(self.bff, bff), _json_schema(self.service, service))

    def test_bff_documents_every_service_status_plus_its_own_upstream_errors(self) -> None:
        service = set(self.operation(self.service)["responses"])
        bff = set(self.operation(self.bff)["responses"])
        self.assertLessEqual(service - {"503"}, bff)
        self.assertLessEqual(bff - service, {"503", "504"})
        self.assertLessEqual({"200", "401", "403", "422"}, service)

    def test_statement_rules_of_adr_036(self) -> None:
        schema = _json_schema(self.bff, self.operation(self.bff)["responses"]["200"])
        properties = schema["properties"]
        self.assertEqual(
            set(properties["statement_status"]["enum"]),
            {"ACTIVE", "PROVISIONING", "NOT_APPLICABLE", "NONE"},
        )
        self.assertEqual(properties["payments_status"]["const"], "NOT_INTEGRATED")
        self.assertEqual(properties["balance"]["type"], "null")
        commitment = _resolved(self.bff, properties["commitments"]["items"])
        self.assertEqual(
            set(commitment["properties"]["status"]["enum"]), {"PENDING_EXTERNAL", "WAIVED"}
        )
        movement = _resolved(self.bff, properties["movements"]["items"])
        self.assertEqual(
            set(movement["properties"]["movement_type"]["enum"]),
            {
                "FREE_ACTIVATED",
                "PLAN_CHANGED",
                "RETURNED_TO_FREE",
                "COMMITMENT_CREATED",
                "COMMITMENT_WAIVED",
            },
        )
        self.assertLessEqual(
            {
                "statement_status",
                "subscription",
                "commitments",
                "movements",
                "next_cursor",
                "payments_status",
                "balance",
            },
            set(schema["required"]),
        )


if __name__ == "__main__":
    unittest.main()
