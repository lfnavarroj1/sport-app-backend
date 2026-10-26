"""HU023 (ADR-040): `bff-web` publica `GET /v1/events` con los mismos parámetros, esquema y
errores que `events-management`, y documenta `GET /v1/me/events/{event_id}/conflicts` con la
forma de ADR-040.

Compara los OpenAPI versionados: el BFF reenvía el calendario sin reinterpretarlo.
"""

import unittest

from test_event_publication_contract import _document, _ok_schema

CALENDAR = ("/v1/events", "get")
CONFLICTS = ("/v1/me/events/{event_id}/conflicts", "get")


class EventCalendarContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bff = _document("bff-web")
        self.service = _document("events-management")

    def test_calendar_matches_the_service(self) -> None:
        path, method = CALENDAR
        bff, service = self.bff["paths"][path][method], self.service["paths"][path][method]
        names = {p["name"] for p in service["parameters"]}

        self.assertEqual({p["name"] for p in bff["parameters"]}, names)
        self.assertLessEqual({"from", "to", "discipline", "city", "limit", "cursor"}, names)
        required = {p["name"] for p in service["parameters"] if p.get("required")}
        self.assertEqual({p["name"] for p in bff["parameters"] if p.get("required")}, required)
        # El BFF agrega sus propios `503`/`504`.
        self.assertLessEqual(set(service["responses"]), set(bff["responses"]))
        self.assertEqual(_ok_schema(self.bff, path, method), _ok_schema(self.service, path, method))
        item = _ok_schema(self.bff, path, method)["properties"]["items"]["items"]
        self.assertNotIn("organizer_id", item["properties"])

    def test_conflicts_shape(self) -> None:
        path, method = CONFLICTS
        operation = self.bff["paths"][path][method]
        schema = _ok_schema(self.bff, path, method)
        conflict = schema["properties"]["conflicts"]["items"]

        self.assertEqual(set(schema["required"]), {"conflicts", "evaluated_at"})
        self.assertEqual(
            set(conflict["required"]),
            {
                "event_id",
                "name",
                "discipline",
                "city",
                "starts_at",
                "ends_at",
                "time_zone",
                "reasons",
            },
        )
        self.assertEqual(
            conflict["properties"]["reasons"]["items"]["enum"], ["SAME_DISCIPLINE", "SAME_CITY"]
        )
        self.assertLessEqual(
            {"200", "401", "403", "404", "503", "504"}, set(operation["responses"])
        )
        self.assertNotIn(path, self.service["paths"])  # composición propia del BFF


if __name__ == "__main__":
    unittest.main()
