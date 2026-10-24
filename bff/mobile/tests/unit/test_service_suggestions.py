"""BFF móvil, HU029 (ADR-041): `GET /v1/me/suggestions/services`.

Borde (`athlete` + `services.support.suggest`), composición de la disponibilidad de Servicios
(ADR-038) con las inscripciones del deportista (ADR-032), timeouts por fuente, `author_name`
por `directory-lookup`, degradación declarada en `sources` y errores `503`/`504` cuando falla
Servicios. Usuarios, Afiliación, Eventos y Servicios se simulan con `httpx.MockTransport`.
"""

import json
import statistics
import time
import unittest
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_capabilities import FREE_CAPABILITIES, SIGNER, Upstream, token

from bff_mobile.main import create_app
from bff_mobile.settings import BffMobileSettings
from sportapp_platform.tokens import public_jwk

SUGGESTIONS = "/v1/me/suggestions/services"
PROVIDER_ID = str(uuid.uuid4())
NOW = datetime.now(UTC).replace(microsecond=0)
# Evento inscrito dentro de 6 días, de 07:00 a 11:00 en Bogotá (UTC-5).
EVENT_DAY = (NOW + timedelta(days=6)).date()


def z(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def at(day_offset: int, hour_utc: int) -> datetime:
    day = datetime.combine(EVENT_DAY, datetime.min.time(), UTC) + timedelta(days=day_offset)
    return day + timedelta(hours=hour_utc)


def window(starts_at: datetime, hours: int = 1) -> dict[str, Any]:
    return {
        "starts_at": z(starts_at),
        "ends_at": z(starts_at + timedelta(hours=hours)),
        "time_zone": "America/Bogota",
        "capacity": 3,
    }


def service(category: str, *intervals: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "service_id": str(uuid.uuid4()),
        "provider_id": PROVIDER_ID,
        "name": f"Servicio {category.lower()}",
        "description": "Servicio sintético de acompañamiento y logística.",
        "category": category,
        "modality": "IN_PERSON",
        "price": {"amount": "45000.00", "currency": "COP", "billing_unit": "SERVICE"},
        "availability": list(intervals),
        "available": True,
    }
    values.update(overrides)
    return values


def registration(starts_at: datetime, hours: int = 4) -> dict[str, Any]:
    return {
        "registration_id": str(uuid.uuid4()),
        "event_id": str(uuid.uuid4()),
        "status": "REGISTERED",
        "registered_at": z(NOW - timedelta(days=1)),
        "applied_fee": None,
        "version": 1,
        "event": {
            "name": "Media maratón",
            "starts_at": z(starts_at),
            "ends_at": z(starts_at + timedelta(hours=hours)),
            "time_zone": "America/Bogota",
            "location": {"venue": "Parque", "city": "Bogotá", "country_code": "CO"},
        },
    }


def error(code: str) -> dict[str, Any]:
    return {"code": code, "message": "m", "correlation_id": None, "details": []}


Reply = httpx.Response | type[Exception]


class Pages:
    """Respuestas paginadas en orden; registra las solicitudes."""

    def __init__(self, *pages: Reply) -> None:
        self.pages = list(pages)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.pages[min(len(self.requests), len(self.pages)) - 1]
        if isinstance(reply, type):
            raise reply("caído")
        return reply

    def params(self, index: int = 0) -> list[tuple[str, str]]:
        return list(self.requests[index].url.params.multi_items())


def page(
    items: list[dict[str, Any]], next_cursor: str | None = None, **extra: Any
) -> httpx.Response:
    return httpx.Response(200, json={"items": items, "next_cursor": next_cursor, **extra})


class Users:
    def __init__(self) -> None:
        self.directory: Reply = httpx.Response(
            200, json={"items": [{"user_id": PROVIDER_ID, "full_name": "Transportes Andinos"}]}
        )
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/jwks.json":
            return httpx.Response(200, json={"keys": [public_jwk(SIGNER.public_key())]})
        self.requests.append(request)
        if isinstance(self.directory, type):
            raise self.directory("usuarios")
        return self.directory


TRANSPORT = service("TRANSPORT", window(at(-3, 10)), window(at(0, 10), hours=2))
COACHING = service("COACHING", window(at(-4, 13)))
MEDICINE = service("SPORTS_MEDICINE", window(at(0, 14)))


class ServiceSuggestionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.affiliation = Upstream(
            {"subscription_status": "ACTIVE", "capabilities": FREE_CAPABILITIES}
        )
        self.users = Users()
        self.services = Pages(page([COACHING, TRANSPORT]))
        self.events = Pages(page([registration(at(0, 12))]))
        self.client = self.client_with(self.services, self.events)
        self.athlete_id = uuid.uuid4()

    def client_with(
        self, services: Callable[[httpx.Request], httpx.Response], events: Pages
    ) -> TestClient:
        return TestClient(
            create_app(
                BffMobileSettings(
                    app_env="test",  # type: ignore[arg-type]
                    users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
                    affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
                    training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
                    events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                    complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
                ),
                users_http=httpx.Client(
                    base_url="http://users", transport=httpx.MockTransport(self.users)
                ),
                affiliation_http=httpx.Client(
                    base_url="http://affiliation",
                    transport=httpx.MockTransport(self.affiliation.affiliation),
                ),
                training_http=httpx.Client(
                    base_url="http://training",
                    transport=httpx.MockTransport(lambda _: httpx.Response(500)),
                ),
                events_http=httpx.Client(
                    base_url="http://events", transport=httpx.MockTransport(events)
                ),
                services_http=httpx.Client(
                    base_url="http://services", transport=httpx.MockTransport(services)
                ),
            )
        )

    def get(self, actor: str | None = "athlete", **params: str) -> httpx.Response:
        headers = {}
        if actor is not None:
            headers["Authorization"] = f"Bearer {token(actor, sub=str(self.athlete_id))}"
        return self.client.get(SUGGESTIONS, params=params, headers=headers)

    def test_logistics_for_the_event_day_first_with_provider_fee_and_interval(self) -> None:
        """CA1 y CA3: transporte del día del evento primero; proveedor, tarifa e intervalo."""
        response = self.get()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body = response.json()
        transport, coaching = body["items"]
        self.assertEqual(
            (transport["service_id"], transport["reasons"]),
            (TRANSPORT["service_id"], ["EVENT_LOGISTICS"]),
        )
        self.assertEqual(
            (coaching["service_id"], coaching["reasons"]),
            (COACHING["service_id"], ["UPCOMING_AVAILABILITY"]),
        )
        self.assertEqual(transport["reference"], {"type": "service", "id": TRANSPORT["service_id"]})
        self.assertEqual(
            (transport["title"], transport["summary"], transport["author_name"]),
            (TRANSPORT["name"], TRANSPORT["description"], "Transportes Andinos"),
        )
        self.assertEqual(
            (transport["category"], transport["modality"], transport["fee"]),
            ("TRANSPORT", "IN_PERSON", TRANSPORT["price"]),
        )
        # El intervalo del día del evento, no el primero del servicio; en UTC con su zona.
        self.assertEqual(
            transport["next_interval"],
            {
                "starts_at": z(at(0, 10)),
                "ends_at": z(at(0, 12)),
                "time_zone": "America/Bogota",
            },
        )
        self.assertNotIn("provider_id", transport)
        self.assertEqual((body["personalized"], body["missing_context"]), (True, []))
        self.assertEqual(
            body["sources"],
            [{"source": "events", "status": "OK"}, {"source": "users", "status": "OK"}],
        )
        self.assertTrue(body["generated_at"].endswith("Z"))

    def test_sources_are_queried_with_the_approved_filters_and_timeouts(self) -> None:
        """ADR-041: 31 días, solo disponibles, tres categorías (nunca medicina deportiva)."""
        self.get()

        sent = dict(self.services.params())
        categories = [value for name, value in self.services.params() if name == "category"]
        self.assertEqual(categories, ["COACHING", "TRANSPORT", "ACCOMPANIMENT"])
        self.assertEqual((sent["availability"], sent["limit"]), ("available", "50"))
        start = datetime.fromisoformat(sent["from"])
        self.assertEqual(datetime.fromisoformat(sent["to"]) - start, timedelta(days=31))
        self.assertTrue(sent["from"].endswith("Z"))
        self.assertLess(abs(start - datetime.now(UTC)), timedelta(minutes=1))
        self.assertTrue(self.services.requests[0].headers["Authorization"].startswith("Bearer "))
        self.assertEqual(self.events.params(), [("status", "REGISTERED"), ("limit", "100")])
        self.assertEqual(self.events.requests[0].extensions["timeout"]["read"], 1.0)
        [lookup] = self.users.requests
        self.assertEqual(json.loads(lookup.content), {"user_ids": [PROVIDER_ID]})
        self.assertEqual(lookup.extensions["timeout"]["read"], 0.3)

    def test_services_are_read_across_all_pages(self) -> None:
        company = service("ACCOMPANIMENT", window(at(0, 20)))
        self.services.pages = [page([COACHING], "c1"), page([company, TRANSPORT])]

        body = self.get().json()

        self.assertEqual(len(self.services.requests), 2)
        self.assertIn(("cursor", "c1"), self.services.params(1))
        self.assertEqual(
            [item["service_id"] for item in body["items"]],
            [TRANSPORT["service_id"], company["service_id"], COACHING["service_id"]],
        )

    def test_sports_medicine_is_never_suggested_even_if_returned(self) -> None:
        self.services.pages = [page([MEDICINE, TRANSPORT])]

        body = self.get().json()

        self.assertEqual([item["category"] for item in body["items"]], ["TRANSPORT"])

    def test_without_registrations_only_coaching_and_missing_context(self) -> None:
        """CA2: sin inscripciones el transporte no se sugiere y el contexto faltante se declara."""
        self.events.pages = [page([])]

        body = self.get().json()

        self.assertEqual([item["service_id"] for item in body["items"]], [COACHING["service_id"]])
        self.assertEqual(
            (body["personalized"], body["missing_context"]), (False, ["event_registrations"])
        )
        self.assertEqual(body["sources"][0], {"source": "events", "status": "OK"})

    def test_without_registrations_nor_services_the_list_is_empty_and_explained(self) -> None:
        """CA2: lista vacía con `missing_context`; nunca servicios arbitrarios."""
        self.events.pages = [page([])]
        self.services.pages = [page([TRANSPORT])]

        body = self.get().json()

        self.assertEqual(body["items"], [])
        self.assertEqual(body["missing_context"], ["event_registrations"])
        self.assertEqual(self.users.requests, [])

    def test_past_and_far_registrations_do_not_count(self) -> None:
        past = registration(NOW - timedelta(days=2))
        far = registration(NOW + timedelta(days=40))
        self.events.pages = [page([past], "e1"), page([far], "e2"), page([])]

        body = self.get().json()

        # La segunda página ya empieza fuera de la ventana: no se pide la tercera.
        self.assertEqual(len(self.events.requests), 2)
        self.assertIn(("cursor", "e1"), self.events.params(1))
        self.assertEqual([item["reasons"] for item in body["items"]], [["UPCOMING_AVAILABILITY"]])
        self.assertEqual(body["missing_context"], ["event_registrations"])

    def test_a_service_without_available_intervals_is_not_suggested(self) -> None:
        """CA4: sin intervalos disponibles deja de sugerirse."""
        self.services.pages = [page([service("COACHING"), TRANSPORT])]

        body = self.get().json()

        self.assertEqual([item["service_id"] for item in body["items"]], [TRANSPORT["service_id"]])

    def test_events_down_returns_the_list_without_the_logistic_reason(self) -> None:
        """CA5 (HA011): si solo falla Eventos, sin motivo logístico y declarado en `sources`."""
        for reply in (httpx.ConnectError, httpx.ReadTimeout, httpx.Response(503, json=error("x"))):
            with self.subTest(reply=reply):
                self.events.pages = [reply]

                response = self.get()

                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(
                    [item["service_id"] for item in body["items"]], [COACHING["service_id"]]
                )
                self.assertEqual(body["sources"][0], {"source": "events", "status": "UNAVAILABLE"})
                self.assertEqual(
                    (body["personalized"], body["missing_context"]),
                    (False, ["event_registrations"]),
                )

    def test_services_down_or_slow_fails_the_request(self) -> None:
        """CA5 (HA011): si Servicios no responde, `503`/`504`."""
        cases: list[tuple[Reply, int, str]] = [
            (httpx.ConnectError, 503, "upstream_unavailable"),
            (httpx.ReadTimeout, 504, "upstream_timeout"),
            (httpx.Response(503, json=error("upstream_unavailable")), 503, "upstream_unavailable"),
            (httpx.Response(422, json=error("validation_error")), 503, "upstream_unavailable"),
            (httpx.Response(200, json={"unexpected": True}), 503, "upstream_unavailable"),
        ]
        for reply, status, code in cases:
            with self.subTest(reply=reply):
                self.services.pages = [reply]

                response = self.get()

                self.assertEqual((response.status_code, response.json()["code"]), (status, code))
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_a_service_outside_the_contract_on_a_later_page_fails_the_request(self) -> None:
        self.services.pages = [page([COACHING], "c1"), page([{"service_id": "x"}])]

        response = self.get()

        self.assertEqual(
            (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
        )

    def test_users_down_leaves_the_provider_name_empty(self) -> None:
        self.users.directory = httpx.ConnectError

        response = self.get()

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(all(item["author_name"] is None for item in body["items"]))
        self.assertEqual(body["sources"][1], {"source": "users", "status": "UNAVAILABLE"})

    def test_limit_truncates_and_is_validated(self) -> None:
        body = self.get(limit="1").json()
        self.assertEqual([item["service_id"] for item in body["items"]], [TRANSPORT["service_id"]])

        for value in ("0", "51", "x"):
            with self.subTest(limit=value):
                response = self.get(limit=value)

                self.assertEqual(response.status_code, 422)
                self.assertEqual(
                    response.json()["details"], [{"field": "query.limit", "issue": "out_of_range"}]
                )

    def test_edge_rejects_other_actors_and_plans_before_calling_the_sources(self) -> None:
        missing = self.get(actor=None)
        organizer = self.get(actor="organizer")
        self.affiliation.snapshot = {"subscription_status": "ACTIVE", "capabilities": []}
        without_capability = self.get()
        self.affiliation.status = 0
        affiliation_down = self.get()

        self.assertEqual(
            (missing.status_code, missing.json()["code"]), (401, "invalid_access_token")
        )
        self.assertEqual(
            (organizer.status_code, organizer.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(
            (without_capability.status_code, without_capability.json()["code"]),
            (403, "capability_required"),
        )
        self.assertEqual(affiliation_down.status_code, 503)
        self.assertEqual((self.services.requests, self.events.requests), ([], []))

    def test_p95_below_two_seconds_with_500_services(self) -> None:
        """HA010 (cálculo del BFF): 500 servicios en 10 páginas de 50 y una inscripción."""
        catalog = [
            service(
                ("COACHING", "TRANSPORT", "ACCOMPANIMENT")[index % 3],
                window(at(index % 5 - 2, 9 + index % 10)),
            )
            for index in range(500)
        ]
        pages = [catalog[start : start + 50] for start in range(0, 500, 50)]

        def services(request: httpx.Request) -> httpx.Response:
            cursor = request.url.params.get("cursor")
            index = int(cursor) if cursor else 0
            return page(pages[index], str(index + 1) if index + 1 < len(pages) else None)

        client = self.client_with(services, Pages(page([registration(at(0, 12))])))
        headers = {"Authorization": f"Bearer {token('athlete', sub=str(self.athlete_id))}"}
        durations = []
        for _ in range(20):
            started = time.perf_counter()
            response = client.get(SUGGESTIONS, params={"limit": "50"}, headers=headers)
            durations.append(time.perf_counter() - started)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(response.json()["items"]), 50)

        self.assertLess(statistics.quantiles(durations, n=20)[18], 2.0)


if __name__ == "__main__":
    unittest.main()
