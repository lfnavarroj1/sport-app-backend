"""HU022 (ADR-039): `bff-web` expone los inscritos con el contrato de `events-management` más la
identidad mínima compuesta (`display_name` y `sources`), y `users-management` publica el
contrato interno de directorio sin correo.

Compara los OpenAPI versionados (sin títulos, descripciones ni ejemplos).
"""

import copy
import unittest
from typing import Any

from test_profile_contract import _json_schema, _load

PATH, METHOD = "/v1/me/events/{event_id}/registrations", "get"
DIRECTORY = "/v1/users/directory-lookup"


class EventAttendeesContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.events = _load("events-management")
        cls.users = _load("users-management")
        cls.bff = _load("bff-web")
        cls.service_operation = cls.events["paths"][PATH][METHOD]
        cls.bff_operation = cls.bff["paths"][PATH][METHOD]

    def test_parameters_are_identical(self) -> None:
        def names(operation: dict[str, Any]) -> set[tuple[str, str]]:
            return {(item["name"], item["in"]) for item in operation["parameters"]}

        self.assertEqual(names(self.bff_operation), names(self.service_operation))
        self.assertLessEqual(
            {("status", "query"), ("limit", "query"), ("cursor", "query")},
            names(self.bff_operation),
        )

    def test_bff_response_is_the_service_response_plus_the_composed_identity(self) -> None:
        service = _json_schema(self.events, self.service_operation["responses"]["200"])
        bff = copy.deepcopy(_json_schema(self.bff, self.bff_operation["responses"]["200"]))

        sources = bff["properties"].pop("sources")
        bff["required"].remove("sources")
        item = bff["properties"]["items"]["items"]
        item["properties"].pop("display_name")
        item["required"].remove("display_name")

        self.assertEqual(bff, service)
        self.assertEqual(sources["items"]["properties"]["source"]["const"], "users")

    def test_bff_documents_every_service_status(self) -> None:
        service = set(self.service_operation["responses"])
        bff = set(self.bff_operation["responses"])

        self.assertLessEqual(service - {"503"}, bff)
        self.assertLessEqual(bff - service, {"503", "504"})

    def test_directory_lookup_exposes_only_the_name(self) -> None:
        """Identidad mínima de ADR-039: `user_id` y `full_name`, nunca el correo."""
        operation = self.users["paths"][DIRECTORY]["post"]
        response = _json_schema(self.users, operation["responses"]["200"])
        item = response["properties"]["items"]["items"]

        self.assertEqual(set(item["properties"]), {"user_id", "full_name"})
        self.assertFalse([path for path in self.bff["paths"] if "directory-lookup" in path])


if __name__ == "__main__":
    unittest.main()
