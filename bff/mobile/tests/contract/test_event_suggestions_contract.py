"""HU027 (ADR-041): `bff-mobile` publica `GET /v1/me/suggestions/events` compuesto sobre los
contratos de `events-management` (`GET /v1/events` de ADR-031 y el detalle de ADR-032).

- El cliente móvil solo envía `limit`: la ventana de 90 días y la paginación las fija el BFF, con
  parámetros que existen en `GET /v1/events`.
- Los campos del evento que el BFF entrega conservan el esquema del detalle de Eventos
  (disciplina, ubicación, instantes, zona, cupo, tarifa e inscripción propia); nunca
  `organizer_id`.
- La tarifa del detalle móvil (`GET /v1/events/{event_id}`) es la de ADR-040,
  `{amount, currency}`, igual que en Eventos (pendiente IC-1 heredado de HU025).
- La respuesta tiene la forma común de ADR-041 y publica los errores `401`/`403`/`422`/`503`/
  `504`.
"""

import unittest
from typing import Any

from test_service_suggestions_contract import _ok, _parameters
from test_training_dashboard_contract import _load

PATH, METHOD = "/v1/me/suggestions/events", "get"
EVENTS, DETAIL = "/v1/events", "/v1/events/{event_id}"
EVENT_FIELDS = (
    "event_id",
    "discipline",
    "location",
    "starts_at",
    "ends_at",
    "time_zone",
    "capacity",
    "fee",
    "my_registration",
)


class EventSuggestionsContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bff = _load("bff-mobile")
        cls.events = _load("events-management")

    def item(self) -> Any:
        return _ok(self.bff, PATH)["properties"]["items"]["items"]["properties"]

    def test_client_only_sends_limit(self) -> None:
        self.assertEqual(_parameters(self.bff, PATH), {"limit", "authorization"})

    def test_parameters_sent_by_the_bff_exist_in_events(self) -> None:
        self.assertLessEqual({"from", "to", "limit", "cursor"}, _parameters(self.events, EVENTS))
        limit = next(
            p for p in self.events["paths"][EVENTS]["get"]["parameters"] if p["name"] == "limit"
        )
        self.assertEqual(limit["schema"]["maximum"], 200)

    def test_event_fields_keep_the_schema_of_the_events_detail(self) -> None:
        item, detail = self.item(), _ok(self.events, DETAIL)["properties"]

        for field in EVENT_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(item[field], detail[field])
        listed = _ok(self.events, EVENTS)["properties"]["items"]["items"]["properties"]
        self.assertEqual(item["fee"], listed["fee"])
        self.assertNotIn("organizer_id", item)

    def test_mobile_event_detail_fee_follows_adr_040(self) -> None:
        """IC-1: `fee {amount, currency}` en el detalle móvil, igual que en Eventos."""
        mobile = _ok(self.bff, DETAIL)["properties"]["fee"]
        source = _ok(self.events, DETAIL)["properties"]["fee"]

        self.assertEqual(mobile, source)
        fee = self.bff["components"]["schemas"]["EventFee"]
        self.assertEqual(set(fee["properties"]), {"amount", "currency"})
        self.assertEqual(set(fee["required"]), {"amount", "currency"})

    def test_item_and_envelope_follow_adr_041(self) -> None:
        body = _ok(self.bff, PATH)

        self.assertEqual(
            set(body["properties"]),
            {"items", "personalized", "missing_context", "generated_at", "sources"},
        )
        self.assertEqual(
            set(body["properties"]["missing_context"]["items"]["enum"]), {"sports", "city"}
        )
        self.assertEqual(
            set(self.item()["reasons"]["items"]["enum"]), {"SPORT_MATCH", "CITY_MATCH", "UPCOMING"}
        )
        self.assertEqual(self.item()["reference"]["properties"]["type"]["const"], "event")
        # Sin resolver: `_resolved` descarta las claves `title` de documentación.
        self.assertLessEqual(
            {"reference", "title", "summary", "reasons"},
            set(self.bff["components"]["schemas"]["EventSuggestionItem"]["properties"]),
        )
        sources = body["properties"]["sources"]["items"]["properties"]["source"]
        self.assertEqual(sources["const"], "users")

    def test_errors_are_published(self) -> None:
        responses = set(self.bff["paths"][PATH][METHOD]["responses"])

        self.assertLessEqual({"200", "401", "403", "422", "503", "504"}, responses)


if __name__ == "__main__":
    unittest.main()
