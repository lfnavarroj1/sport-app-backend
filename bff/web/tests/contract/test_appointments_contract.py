"""HU040 (ADR-046 §4): `bff-web` publica `GET /v1/me/appointments` para el profesional con
los mismos parámetros y campos que `complementary-services` 0.6.0, más
`athlete_display_name` (directorio de Usuarios) y `sources`. Si el servicio retira o renombra
un campo reenviado, esta prueba falla antes que la composición en producción."""

import unittest
from typing import Any

from test_profile_contract import _load, _resolved

PATH = "/v1/me/appointments"
PARAMETERS = {"from", "to", "status", "limit", "cursor"}


def _page(document: dict[str, Any]) -> dict[str, Any]:
    response = document["paths"][PATH]["get"]["responses"]["200"]["content"]["application/json"]
    page: dict[str, Any] = _resolved(document, response["schema"])
    return page


def _item(document: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = _resolved(document, _page(document)["properties"]["items"]["items"])
    return item


def _query(document: dict[str, Any]) -> set[str]:
    return {
        item["name"]
        for item in document["paths"][PATH]["get"]["parameters"]
        if item["in"] == "query"
    }


class ProviderAppointmentsContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bff = _load("bff-web")
        cls.services = _load("complementary-services")

    def test_same_query_parameters(self) -> None:
        self.assertEqual(_query(self.bff), PARAMETERS)
        self.assertLessEqual(PARAMETERS, _query(self.services))

    def test_item_forwards_service_fields_and_adds_the_display_name(self) -> None:
        bff, service = _item(self.bff), _item(self.services)

        self.assertEqual(set(bff["required"]) - set(service["required"]), {"athlete_display_name"})
        self.assertLessEqual(set(service["required"]), set(bff["required"]))
        self.assertEqual(
            set(bff["properties"]["status"]["enum"]), set(service["properties"]["status"]["enum"])
        )
        self.assertEqual(bff["properties"]["meeting_url"]["type"], "null")
        self.assertNotIn("email", bff["properties"])

    def test_page_and_errors(self) -> None:
        page = _page(self.bff)
        self.assertEqual(set(page["required"]), {"items", "next_cursor", "sources"})
        responses = set(self.bff["paths"][PATH]["get"]["responses"])
        self.assertLessEqual({"200", "401", "403", "422", "503", "504"}, responses)
        service_responses = set(self.services["paths"][PATH]["get"]["responses"])
        self.assertLessEqual({"200", "401", "403", "422", "503"}, service_responses)
        self.assertNotIn("post", self.bff["paths"][PATH], "la reserva es del canal móvil")


if __name__ == "__main__":
    unittest.main()
