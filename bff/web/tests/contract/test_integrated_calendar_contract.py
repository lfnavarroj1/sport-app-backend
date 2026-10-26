"""HU018 (ADR-039): `bff-web` publica `GET /v1/me/integrated-calendar` y compone solo campos
que existen en los contratos versionados de las consultas que reutiliza (Eventos, Servicios y
Entrenamientos), sin consultas nuevas en los microservicios.

Si un servicio retira o renombra un campo consumido, esta prueba falla antes que la
composición en producción.
"""

import unittest
from typing import Any

from test_profile_contract import _load, _resolved

PATH = "/v1/me/integrated-calendar"
SOURCES = {
    "OWN_EVENT",
    "EVENT_PUBLISHED",
    "EVENT_REGISTRATION",
    "TRAINING_PLAN_SESSION",
    "OWN_SERVICE_AVAILABILITY",
    "ASSOCIATED_SERVICE_AVAILABILITY",
    # HU040 (ADR-046 §4): citas de los servicios del profesional.
    "OWN_APPOINTMENT",
}
STATUSES = {
    "DRAFT",
    "PUBLISHED",
    "AVAILABLE",
    "ASSIGNED",
    "REGISTERED",
    "BOOKED",
}


def _operation(document: dict[str, Any], path: str) -> dict[str, Any]:
    operation: dict[str, Any] = document["paths"][path]["get"]
    return operation


def _parameters(operation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in operation["parameters"] if item["in"] == "query"}


def _item(document: dict[str, Any], path: str) -> dict[str, Any]:
    """Elemento de `items` de la respuesta `200` de una consulta paginada."""
    response = _operation(document, path)["responses"]["200"]["content"]["application/json"]
    page = _resolved(document, response["schema"])
    item: dict[str, Any] = page["properties"]["items"]["items"]
    assert {"items", "next_cursor"} <= set(page["required"])
    return item


def _maximum(parameter: dict[str, Any]) -> int | None:
    schema = parameter["schema"]
    for option in [schema, *schema.get("anyOf", [])]:
        if "maximum" in option:
            return int(option["maximum"])
    return None


class IntegratedCalendarContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bff = _load("bff-web")
        cls.events = _load("events-management")
        cls.services = _load("complementary-services")
        cls.training = _load("training-management")

    def assert_fields(self, item: dict[str, Any], fields: set[str]) -> None:
        self.assertLessEqual(fields, set(item["required"]))

    def test_bff_operation(self) -> None:
        operation = _operation(self.bff, PATH)
        self.assertEqual(set(_parameters(operation)), {"from", "to"})
        self.assertEqual(set(operation["responses"]), {"200", "401", "403", "422", "503", "504"})
        body = _resolved(
            self.bff, operation["responses"]["200"]["content"]["application/json"]["schema"]
        )
        self.assertEqual(set(body["required"]), {"from", "to", "items", "sources", "evaluated_at"})
        variants = body["properties"]["items"]["items"]["oneOf"]
        by_source = {variant["properties"]["source"]["const"]: variant for variant in variants}
        self.assertEqual(set(by_source), SOURCES)
        statuses: set[str] = set()
        for source, variant in by_source.items():
            with self.subTest(source=source):
                self.assertLessEqual(
                    {
                        "item_id",
                        "source",
                        "status",
                        "occurs_on",
                        "title",
                        "starts_at",
                        "ends_at",
                        "time_zone",
                        "reference",
                    },
                    set(variant["required"]),
                )
                status = variant["properties"]["status"]
                statuses |= set(status.get("enum", [status.get("const")]))
                self.assertNotIn("organizer_id", variant["properties"])
                self.assertNotIn("provider_id", variant["properties"])
                self.assertNotIn("athlete_id", variant["properties"])
        self.assertEqual(statuses, STATUSES)
        source = body["properties"]["sources"]["items"]
        self.assertEqual(set(source["properties"]["name"]["enum"]), SOURCES)
        self.assertEqual(
            set(source["properties"]["status"]["enum"]), {"OK", "UNAVAILABLE", "TIMEOUT"}
        )

    def test_events_dependencies(self) -> None:
        own = _parameters(_operation(self.events, "/v1/me/events"))
        self.assertGreaterEqual(_maximum(own["limit"]) or 0, 100)
        self.assertIn("cursor", own)
        self.assert_fields(
            _item(self.events, "/v1/me/events"),
            {
                "event_id",
                "name",
                "discipline",
                "location",
                "starts_at",
                "ends_at",
                "time_zone",
                "status",
            },
        )
        published = _parameters(_operation(self.events, "/v1/events"))
        self.assertLessEqual({"from", "to", "limit"}, set(published))
        self.assertEqual(_maximum(published["limit"]), 200)
        self.assert_fields(
            _item(self.events, "/v1/events"),
            {
                "event_id",
                "name",
                "discipline",
                "location",
                "starts_at",
                "ends_at",
                "time_zone",
                "status",
            },
        )
        registration = _item(self.events, "/v1/me/event-registrations")
        self.assert_fields(registration, {"registration_id", "event_id", "status", "event"})
        self.assert_fields(
            registration["properties"]["event"],
            {"name", "starts_at", "ends_at", "time_zone", "location"},
        )
        self.assertIn("cursor", _parameters(_operation(self.events, "/v1/me/event-registrations")))

    def test_services_dependencies(self) -> None:
        own = _item(self.services, "/v1/me/services")
        self.assert_fields(
            own, {"service_id", "name", "category", "modality", "status", "availability"}
        )
        self.assertEqual(set(own["properties"]["status"]["enum"]), {"DRAFT", "PUBLISHED"})
        interval = own["properties"]["availability"]["items"]
        self.assert_fields(interval, {"starts_at", "ends_at", "time_zone", "capacity"})
        association = _item(self.services, "/v1/me/service-associations")
        self.assert_fields(association, {"association_id", "service_id", "service"})
        self.assert_fields(association["properties"]["service"], {"category"})
        catalog = _operation(self.services, "/v1/services/availability")
        self.assertLessEqual(
            {"from", "to", "category", "availability", "limit", "cursor"}, set(_parameters(catalog))
        )
        offer = _item(self.services, "/v1/services/availability")
        self.assert_fields(offer, {"service_id", "name", "category", "modality", "availability"})
        appointments = _operation(self.services, "/v1/me/appointments")
        parameters = _parameters(appointments)
        self.assertLessEqual({"from", "to", "limit", "cursor"}, set(parameters))
        appointment = _item(self.services, "/v1/me/appointments")
        self.assert_fields(
            appointment,
            {
                "appointment_id",
                "athlete_id",
                "service_id",
                "availability_id",
                "service",
                "starts_at",
                "ends_at",
                "time_zone",
                "status",
            },
        )
        self.assert_fields(appointment["properties"]["service"], {"name", "category", "modality"})

    def test_training_dependencies(self) -> None:
        parameters = _parameters(_operation(self.training, "/v1/me/training-plan-sessions"))
        self.assertLessEqual({"from", "to", "limit"}, set(parameters))
        self.assertEqual(_maximum(parameters["limit"]), 200)
        self.assert_fields(
            _item(self.training, "/v1/me/training-plan-sessions"),
            {
                "plan_id",
                "plan_title",
                "session_id",
                "scheduled_date",
                "sport",
                "duration_minutes",
                "rpe",
            },
        )


if __name__ == "__main__":
    unittest.main()
