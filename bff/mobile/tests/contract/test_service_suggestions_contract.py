"""HU029 (ADR-041): `bff-mobile` publica `GET /v1/me/suggestions/services` compuesto sobre los
contratos de `complementary-services` (ADR-038) y `events-management` (ADR-032).

- El cliente móvil solo envía `limit`: ventana, categorías y disponibilidad las fija el BFF.
- Los campos del servicio que el BFF entrega conservan el esquema de Servicios (categoría,
  modalidad, tarifa e intervalo) y la categoría nunca admite `SPORTS_MEDICINE`.
- Los parámetros que el BFF envía existen en los contratos de las fuentes.
- La respuesta tiene la forma común de ADR-041 y publica los errores `401`/`403`/`422`/`503`/
  `504`.
"""

import unittest
from typing import Any

from test_training_dashboard_contract import _load, _resolved

PATH, METHOD = "/v1/me/suggestions/services", "get"
AVAILABILITY = "/v1/services/availability"
REGISTRATIONS = "/v1/me/event-registrations"


def _ok(document: dict[str, Any], path: str = PATH) -> Any:
    operation = document["paths"][path]["get"]
    return _resolved(
        document, operation["responses"]["200"]["content"]["application/json"]["schema"]
    )


def _parameters(document: dict[str, Any], path: str) -> set[str]:
    return {p["name"] for p in document["paths"][path]["get"].get("parameters", [])}


class ServiceSuggestionsContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bff = _load("bff-mobile")
        cls.services = _load("complementary-services")
        cls.events = _load("events-management")

    def item(self) -> Any:
        return _ok(self.bff)["properties"]["items"]["items"]["properties"]

    def catalog_service(self) -> Any:
        return _ok(self.services, AVAILABILITY)["properties"]["items"]["items"]["properties"]

    def test_client_only_sends_limit(self) -> None:
        self.assertEqual(_parameters(self.bff, PATH), {"limit", "authorization"})

    def test_parameters_sent_by_the_bff_exist_in_the_sources(self) -> None:
        self.assertLessEqual(
            {"from", "to", "category", "availability", "limit", "cursor"},
            _parameters(self.services, AVAILABILITY),
        )
        self.assertLessEqual({"status", "limit", "cursor"}, _parameters(self.events, REGISTRATIONS))

    def test_service_fields_keep_the_schema_of_complementary_services(self) -> None:
        item, source = self.item(), self.catalog_service()

        self.assertEqual(item["modality"], source["modality"])
        self.assertEqual(item["fee"]["required"], source["price"]["required"])
        for field in ("currency", "billing_unit"):
            with self.subTest(field=field):
                self.assertEqual(
                    item["fee"]["properties"][field], source["price"]["properties"][field]
                )
        interval = source["availability"]["items"]["properties"]
        for field in ("starts_at", "ends_at", "time_zone"):
            with self.subTest(field=field):
                self.assertEqual(
                    item["next_interval"]["properties"][field]["type"], interval[field]["type"]
                )
        self.assertEqual(item["service_id"], source["service_id"])

    def test_category_is_a_subset_without_sports_medicine(self) -> None:
        categories = set(self.item()["category"]["enum"])

        self.assertEqual(categories, {"COACHING", "TRANSPORT", "ACCOMPANIMENT"})
        self.assertLessEqual(categories, set(self.catalog_service()["category"]["enum"]))

    def test_item_and_envelope_follow_adr_041(self) -> None:
        body = _ok(self.bff)

        self.assertEqual(
            set(body["properties"]),
            {"items", "personalized", "missing_context", "generated_at", "sources"},
        )
        self.assertEqual(
            body["properties"]["missing_context"]["items"]["const"], "event_registrations"
        )
        self.assertEqual(
            set(self.item()["reasons"]["items"]["enum"]),
            {"EVENT_LOGISTICS", "UPCOMING_AVAILABILITY"},
        )
        # Sin resolver: `_resolved` descarta las claves `title` de documentación.
        self.assertLessEqual(
            {"reference", "title", "summary", "reasons", "author_name", "next_interval", "fee"},
            set(self.bff["components"]["schemas"]["ServiceSuggestionItem"]["properties"]),
        )
        self.assertNotIn("provider_id", self.item())
        sources = body["properties"]["sources"]["items"]["properties"]["source"]["enum"]
        self.assertEqual(set(sources), {"events", "users"})

    def test_errors_are_published(self) -> None:
        responses = set(self.bff["paths"][PATH][METHOD]["responses"])

        self.assertLessEqual({"200", "401", "403", "422", "503", "504"}, responses)


if __name__ == "__main__":
    unittest.main()
