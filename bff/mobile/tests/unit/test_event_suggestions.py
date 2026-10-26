"""BFF móvil, HU027 (ADR-041): `GET /v1/me/suggestions/events`.

Borde (`athlete` + `events.suggest`), composición de `GET /v1/events` (90 días, páginas de 200)
con el contexto interno de Usuarios (timeout 300 ms, caché) y el detalle de cada evento devuelto
(`capacity`, `fee`, `my_registration`), degradación a no personalizada cuando falla Usuarios y
errores `503`/`504` cuando falla Eventos. Usuarios, Afiliación y Eventos se simulan con
`httpx.MockTransport`.
"""

import sys
import unittest
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

import httpx
from fastapi.testclient import TestClient
from test_capabilities import FREE_CAPABILITIES, SIGNER, Upstream, token

from bff_mobile.main import create_app
from bff_mobile.settings import BffMobileSettings
from sportapp_platform.tokens import public_jwk

SUGGESTIONS = "/v1/me/suggestions/events"
NOW = datetime.now(UTC).replace(microsecond=0)
CONTEXT = {
    "user_id": str(uuid.uuid4()),
    "sports": ["swimming"],
    "primary_goal": "HEALTH",
    "city": "Bogotá",
    "country_code": "CO",
    "birth_date": "1990-01-01",
}


def z(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def event(discipline: str, city: str, days: float, **overrides: Any) -> dict[str, Any]:
    starts_at = NOW + timedelta(days=days)
    values: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "name": f"{discipline} en {city}",
        "discipline": discipline,
        "location": {"name": "Parque", "city": city, "country_code": "CO"},
        "starts_at": z(starts_at),
        "ends_at": z(starts_at + timedelta(hours=3)),
        "time_zone": "America/Bogota",
        "status": "PUBLISHED",
        "version": 2,
        "fee": None,
    }
    values.update(overrides)
    return values


def detail_of(item: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    values = {
        **item,
        "description": "Evento sintético para las sugerencias.",
        "capacity": 150,
        "my_registration": None,
        "registration_count": None,
    }
    values.update(overrides)
    return values


def error(code: str) -> dict[str, Any]:
    return {"code": code, "message": "m", "correlation_id": None, "details": []}


Reply = httpx.Response | type[Exception]


class Events:
    """`GET /v1/events` paginado por cursor y detalle por evento; registra las solicitudes."""

    def __init__(self, *pages: Reply) -> None:
        self.pages = list(pages)
        self.details: dict[str, Reply] = {}
        self.listing: list[httpx.Request] = []
        self.detail_requests: list[httpx.Request] = []

    def page(self, items: list[dict[str, Any]], next_cursor: str | None = None) -> None:
        self.pages.append(httpx.Response(200, json={"items": items, "next_cursor": next_cursor}))
        for item in items:
            self.details.setdefault(item["event_id"], httpx.Response(200, json=detail_of(item)))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/events":
            self.listing.append(request)
            # Sin cursor, la primera página; con `c<n>`, la página n.
            cursor = request.url.params.get("cursor")
            reply = self.pages[int(cursor[1:]) if cursor else 0]
        else:
            self.detail_requests.append(request)
            reply = self.details.get(request.url.path.rsplit("/", 1)[-1], httpx.Response(404))
        if isinstance(reply, type):
            raise reply("caído")
        return reply


class Users:
    def __init__(self) -> None:
        self.context: Reply = httpx.Response(200, json=CONTEXT)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/jwks.json":
            return httpx.Response(200, json={"keys": [public_jwk(SIGNER.public_key())]})
        self.requests.append(request)
        if isinstance(self.context, type):
            raise self.context("usuarios")
        return self.context


SWIM_BOGOTA = event("Natación", "Bogotá", 30)
RUN_BOGOTA = event("Carrera", "bogota", 5)
SWIM_CALI = event("Aguas abiertas", "Cali", 2)
CHESS = event("Ajedrez", "Medellín", 1)


class EventSuggestionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.affiliation = Upstream(
            {"subscription_status": "ACTIVE", "capabilities": FREE_CAPABILITIES}
        )
        self.users = Users()
        self.events = Events()
        self.events.page([CHESS, SWIM_CALI], next_cursor="c1")
        self.events.page([RUN_BOGOTA, SWIM_BOGOTA])
        self.athlete_id = uuid.uuid4()
        self.client = self.client_with(self.events)

    def client_with(self, events: Callable[[httpx.Request], httpx.Response]) -> TestClient:
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
                    base_url="http://services",
                    transport=httpx.MockTransport(lambda _: httpx.Response(500)),
                ),
            )
        )

    def get(self, actor: str | None = "athlete", **params: str) -> httpx.Response:
        headers = {}
        if actor is not None:
            headers["Authorization"] = f"Bearer {token(actor, sub=str(self.athlete_id))}"
        return self.client.get(SUGGESTIONS, params=params, headers=headers)

    def test_personalized_order_reasons_and_content(self) -> None:
        """CA1 y CA4: deporte y ciudad primero, luego por fecha; motivo, fecha, zona, ciudad,
        cupo y tarifa."""
        response = self.get()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body = response.json()
        self.assertEqual(
            [(item["event_id"], item["reasons"]) for item in body["items"]],
            [
                (SWIM_BOGOTA["event_id"], ["SPORT_MATCH", "CITY_MATCH"]),
                (SWIM_CALI["event_id"], ["SPORT_MATCH"]),
                (RUN_BOGOTA["event_id"], ["CITY_MATCH"]),
                (CHESS["event_id"], ["UPCOMING"]),
            ],
        )
        first = body["items"][0]
        self.assertEqual(first["reference"], {"type": "event", "id": SWIM_BOGOTA["event_id"]})
        self.assertEqual(
            (first["title"], first["summary"], first["discipline"], first["capacity"]),
            ("Natación en Bogotá", "Evento sintético para las sugerencias.", "Natación", 150),
        )
        self.assertEqual(first["location"]["city"], "Bogotá")
        self.assertEqual(first["starts_at"], SWIM_BOGOTA["starts_at"])
        self.assertEqual(first["time_zone"], "America/Bogota")
        self.assertIsNone(first["fee"])
        self.assertIsNone(first["my_registration"])
        self.assertNotIn("organizer_id", first)
        self.assertEqual((body["personalized"], body["missing_context"]), (True, []))
        self.assertEqual(body["sources"], [{"source": "users", "status": "OK"}])
        self.assertEqual(datetime.fromisoformat(body["generated_at"]).tzinfo, UTC)

    def test_bff_asks_events_for_the_next_90_days_in_pages_of_200(self) -> None:
        self.assert_asks_the_next_90_days_in_pages_of_200()

    def test_window_starts_at_the_request_even_long_after_the_import(self) -> None:
        """IC-6: la ventana se compara con el instante de la solicitud y no con el `NOW` del
        módulo, que con la suite completa se tomó minutos antes."""
        with mock.patch.object(sys.modules[__name__], "NOW", NOW - timedelta(hours=1)):
            self.assert_asks_the_next_90_days_in_pages_of_200()

    def assert_asks_the_next_90_days_in_pages_of_200(self) -> None:
        # El BFF trunca el instante a segundos: se trunca `before` y no `after`.
        before = datetime.now(UTC).replace(microsecond=0)
        self.get()
        after = datetime.now(UTC)

        first, second = (dict(request.url.params) for request in self.events.listing)
        self.assertEqual(first["limit"], "200")
        self.assertNotIn("cursor", first)
        self.assertEqual(second["cursor"], "c1")
        start, end = (datetime.fromisoformat(first[key]) for key in ("from", "to"))
        self.assertLessEqual(before, start)
        self.assertLessEqual(start, after)
        self.assertEqual(end - start, timedelta(days=90))
        self.assertTrue(first["from"].endswith("Z"))
        for request in [*self.events.listing, *self.events.detail_requests]:
            self.assertTrue(request.headers["Authorization"].startswith("Bearer "))

    def test_fee_and_my_registration_come_from_the_detail(self) -> None:
        """CA3 y CA4: tarifa de ADR-040 y el evento inscrito marcado con `my_registration`."""
        registration = {
            "registration_id": str(uuid.uuid4()),
            "status": "REGISTERED",
            "registered_at": z(NOW - timedelta(days=1)),
        }
        fee = {"amount": "85000.00", "currency": "COP"}
        self.events.details[SWIM_BOGOTA["event_id"]] = httpx.Response(
            200, json=detail_of(SWIM_BOGOTA, fee=fee, my_registration=registration)
        )

        first = self.get().json()["items"][0]

        self.assertEqual(first["fee"], fee)
        self.assertEqual(first["my_registration"], registration)

    def test_started_events_and_vanished_details_are_left_out(self) -> None:
        """CA3: un evento ya iniciado (el periodo de Eventos es de superposición) o que dejó de
        estar publicado entre ambas lecturas (`404`) no aparece."""
        started = event("Natación", "Bogotá", -0.01)
        self.events.pages.clear()
        self.events.page([started, SWIM_CALI, SWIM_BOGOTA])
        self.events.details[SWIM_CALI["event_id"]] = httpx.Response(
            404, json=error("event_not_found")
        )

        body = self.get().json()

        self.assertEqual([item["event_id"] for item in body["items"]], [SWIM_BOGOTA["event_id"]])
        self.assertNotIn(
            f"/v1/events/{started['event_id']}",
            [request.url.path for request in self.events.detail_requests],
        )

    def test_limit_truncates_before_asking_the_detail(self) -> None:
        response = self.get(limit="2")

        self.assertEqual(
            [item["event_id"] for item in response.json()["items"]],
            [SWIM_BOGOTA["event_id"], SWIM_CALI["event_id"]],
        )
        self.assertEqual(len(self.events.detail_requests), 2)

    def test_missing_context_is_not_personalized(self) -> None:
        """CA2: sin deportes o sin ciudad, próximos eventos por fecha con `missing_context`."""
        for overrides, missing in (
            ({"sports": []}, ["sports"]),
            ({"city": None, "country_code": None}, ["city"]),
            ({"sports": [], "city": None, "country_code": None}, ["sports", "city"]),
        ):
            with self.subTest(missing=missing):
                self.users.context = httpx.Response(200, json={**CONTEXT, **overrides})
                self.athlete_id = uuid.uuid4()  # sin caché previa

                body = self.get().json()

                self.assertEqual(
                    [item["event_id"] for item in body["items"]],
                    [
                        CHESS["event_id"],
                        SWIM_CALI["event_id"],
                        RUN_BOGOTA["event_id"],
                        SWIM_BOGOTA["event_id"],
                    ],
                )
                self.assertTrue(all(item["reasons"] == ["UPCOMING"] for item in body["items"]))
                self.assertEqual((body["personalized"], body["missing_context"]), (False, missing))
                self.assertEqual(body["sources"], [{"source": "users", "status": "OK"}])

    def test_users_down_or_slow_degrades_to_not_personalized(self) -> None:
        """CA2 (HA011): sin respuesta de Usuarios, lista por fecha declarada en `sources`."""
        for failure in (httpx.ConnectError, httpx.ReadTimeout):
            with self.subTest(failure=failure.__name__):
                self.users.context = failure
                self.athlete_id = uuid.uuid4()

                response = self.get()

                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(
                    (body["personalized"], body["missing_context"]), (False, ["sports", "city"])
                )
                self.assertEqual(body["sources"], [{"source": "users", "status": "UNAVAILABLE"}])
                self.assertEqual(body["items"][0]["event_id"], CHESS["event_id"])

    def test_unknown_athlete_context_is_not_personalized(self) -> None:
        self.users.context = httpx.Response(404, json=error("user_not_found"))

        body = self.get().json()

        self.assertEqual(
            (body["personalized"], body["missing_context"]), (False, ["sports", "city"])
        )
        self.assertEqual(body["sources"], [{"source": "users", "status": "OK"}])

    def test_context_is_cached_per_athlete(self) -> None:
        self.get()
        self.get()

        self.assertEqual(len(self.users.requests), 1)
        self.assertEqual(
            self.users.requests[0].url.path, f"/v1/users/{self.athlete_id}/athlete-context"
        )

    def test_events_failures_are_503_or_504(self) -> None:
        """CA5 (HA011): Eventos caído, lento o fuera de contrato → `503`/`504`."""
        cases: list[tuple[Reply, int, str]] = [
            (httpx.ConnectError, 503, "upstream_unavailable"),
            (httpx.ReadTimeout, 504, "upstream_timeout"),
            (httpx.Response(503, json=error("upstream_unavailable")), 503, "upstream_unavailable"),
            (httpx.Response(500), 503, "upstream_unavailable"),
            (httpx.Response(200, json={"unexpected": True}), 503, "upstream_unavailable"),
        ]
        for reply, status, code in cases:
            with self.subTest(code=code, status=status, reply=reply):
                events = Events(reply)
                self.client = self.client_with(events)

                response = self.get()

                self.assertEqual((response.status_code, response.json()["code"]), (status, code))
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_detail_failure_is_503_or_504(self) -> None:
        for reply, status in (
            (httpx.ReadTimeout, 504),
            (httpx.Response(500), 503),
            (httpx.Response(200, json={"event_id": SWIM_BOGOTA["event_id"]}), 503),
        ):
            with self.subTest(status=status):
                self.events.details[SWIM_BOGOTA["event_id"]] = reply

                self.assertEqual(self.get().status_code, status)

    def test_events_capability_rejection_is_relayed(self) -> None:
        events = Events(httpx.Response(403, json=error("capability_required")))
        self.client = self.client_with(events)

        response = self.get()

        self.assertEqual(
            (response.status_code, response.json()["code"]), (403, "capability_required")
        )

    def test_edge_rejects_without_token_other_actors_and_without_capability(self) -> None:
        unauthenticated = self.get(actor=None)
        organizer = self.get(actor="organizer")
        self.affiliation.snapshot = {
            "subscription_status": "ACTIVE",
            "capabilities": [c for c in FREE_CAPABILITIES if c != "events.suggest"],
        }
        without_capability = self.get()

        self.assertEqual(
            (unauthenticated.status_code, unauthenticated.json()["code"]),
            (401, "invalid_access_token"),
        )
        self.assertEqual(
            (organizer.status_code, organizer.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(
            (without_capability.status_code, without_capability.json()["code"]),
            (403, "capability_required"),
        )
        self.assertEqual(self.events.listing, [])

    def test_invalid_limit_is_422(self) -> None:
        for value in ("0", "51", "diez"):
            with self.subTest(limit=value):
                response = self.get(limit=value)

                self.assertEqual(response.status_code, 422)
                self.assertEqual(
                    response.json()["details"], [{"field": "query.limit", "issue": "out_of_range"}]
                )


if __name__ == "__main__":
    unittest.main()
