"""BFF web, HU023: filtro de borde, reenvío del calendario y composición de coincidencias
(ADR-040) contra un `events-management` simulado."""

import unittest
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_training import Upstreams, token

from bff_web.main import create_app
from bff_web.settings import BffWebSettings

START = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def event(
    starts_at: datetime,
    *,
    hours: float = 2,
    discipline: str = "Running",
    city: str = "Bogotá",
    status: str = "PUBLISHED",
) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "name": f"Evento {discipline} {city}",
        "discipline": discipline,
        "location": {"name": "Parque central", "city": city, "country_code": "CO"},
        "starts_at": iso(starts_at),
        "ends_at": iso(starts_at + timedelta(hours=hours)),
        "time_zone": "America/Bogota",
        "status": status,
        "version": 1,
    }


def error(code: str) -> dict[str, Any]:
    return {"code": code, "message": "m", "correlation_id": None, "details": []}


class FakeEvents:
    """`GET /v1/me/events` y `GET /v1/events` con paginación por desplazamiento; el filtro
    compara recortado y en minúsculas (la normalización real es del servicio)."""

    def __init__(self) -> None:
        self.mine: list[dict[str, Any]] = []
        self.published: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.status: dict[str, int] = {}
        self.failure: type[Exception] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.failure is not None:
            raise self.failure("eventos")
        self.requests.append(request)
        path, query = request.url.path, dict(request.url.params)
        if path in self.status:
            return httpx.Response(self.status[path], json=error("service_error"))
        items = self.mine if path == "/v1/me/events" else self._published(query)
        offset, limit = int(query.get("cursor", "0")), int(query["limit"])
        following = offset + limit
        return httpx.Response(
            200,
            json={
                "items": items[offset:following],
                "next_cursor": str(following) if following < len(items) else None,
            },
        )

    def _published(self, query: dict[str, str]) -> list[dict[str, Any]]:
        period_from, period_to = query["from"], query["to"]
        return [
            item
            for item in self.published
            if item["starts_at"] <= period_to
            and item["ends_at"] >= period_from
            and query.get("discipline", item["discipline"]).strip().lower()
            == item["discipline"].lower()
            and query.get("city", item["location"]["city"]).strip().lower()
            == item["location"]["city"].lower()
        ]

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


class EventCalendarRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.events = FakeEvents()
        self.client = TestClient(
            create_app(
                BffWebSettings(
                    app_env="test",  # type: ignore[arg-type]
                    users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
                    affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
                    training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
                    complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
                    events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                ),
                users_http=httpx.Client(
                    transport=httpx.MockTransport(Upstreams().users), base_url="http://users"
                ),
                events_http=httpx.Client(
                    transport=httpx.MockTransport(self.events), base_url="http://events"
                ),
            )
        )
        self.own = event(START, hours=3, discipline="Running", city="Bogotá", status="DRAFT")
        self.events.mine = [self.own]

    def auth(self, actor: str | None = "organizer") -> dict[str, str]:
        return {"Authorization": f"Bearer {token(actor)}"}

    def calendar(self, actor: str | None = "organizer", **params: str) -> Any:
        query = {"from": "2026-11-01T00:00:00Z", "to": "2026-12-01T00:00:00Z", **params}
        return self.client.get("/v1/events", params=query, headers=self.auth(actor))

    def conflicts(self, event_id: str | None = None, actor: str | None = "organizer") -> Any:
        return self.client.get(
            f"/v1/me/events/{event_id or self.own['event_id']}/conflicts", headers=self.auth(actor)
        )

    def test_calendar_forwards_parameters_and_body(self) -> None:
        """CA1/CA4: periodo, filtros, límite y cursor viajan tal cual; cuerpo sin cambios."""
        self.events.published = [event(START)]

        response = self.calendar(discipline=" RUNNING ", city="bogotá", limit="50", cursor="0")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"], self.events.published)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        [sent] = self.events.requests
        self.assertEqual(
            dict(sent.url.params),
            {
                "from": "2026-11-01T00:00:00Z",
                "to": "2026-12-01T00:00:00Z",
                "discipline": " RUNNING ",
                "city": "bogotá",
                "limit": "50",
                "cursor": "0",
            },
        )
        self.assertTrue(sent.headers["Authorization"].startswith("Bearer "))

    def test_calendar_service_errors(self) -> None:
        """CA3: el `422` del servicio (periodo inválido) llega sin alterar; otro estado es 503."""
        for status, expected in ((422, 422), (403, 403), (500, 503)):
            with self.subTest(status=status):
                self.events.status["/v1/events"] = status
                response = self.calendar()
                self.assertEqual(response.status_code, expected)
                if status != 500:
                    self.assertEqual(response.json()["code"], "service_error")

    def test_edge_filter_on_both_operations(self) -> None:
        """Solo `organizer` con `events.calendar.view`; los demás no llegan al servicio."""
        for actor in ("athlete", "service_provider", None):
            with self.subTest(actor=actor):
                for response in (self.calendar(actor), self.conflicts(actor=actor)):
                    self.assertEqual(
                        (response.status_code, response.json()["code"]),
                        (403, "permission_required"),
                    )
        anonymous = self.client.get(f"/v1/me/events/{self.own['event_id']}/conflicts")
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(self.events.requests, [])

    def test_conflicts_reasons_borders_and_order(self) -> None:
        """CA2: solapamiento estricto con disciplina o ciudad; motivos; sin el propio evento."""
        own_start, own_end = START, START + timedelta(hours=3)
        both = event(own_start + timedelta(hours=2), discipline="running", city="BOGOTÁ")
        by_city = event(own_start - timedelta(hours=1), discipline="Ciclismo", city="Bogotá")
        by_discipline = event(own_start + timedelta(hours=1), discipline="Running", city="Cali")
        unrelated = event(own_start, discipline="Natación", city="Cali")
        touching_end = event(own_end, city="Bogotá")
        touching_start = event(own_start - timedelta(hours=2), city="Bogotá")
        self.events.published = [
            both,
            by_city,
            by_discipline,
            unrelated,
            touching_end,
            touching_start,
        ]

        response = self.conflicts()

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(
            [(c["event_id"], c["reasons"]) for c in body["conflicts"]],
            [
                (by_city["event_id"], ["SAME_CITY"]),
                (by_discipline["event_id"], ["SAME_DISCIPLINE"]),
                (both["event_id"], ["SAME_DISCIPLINE", "SAME_CITY"]),
            ],
        )
        self.assertEqual(
            body["conflicts"][0],
            {
                "event_id": by_city["event_id"],
                "name": by_city["name"],
                "discipline": "Ciclismo",
                "city": "Bogotá",
                "starts_at": by_city["starts_at"],
                "ends_at": by_city["ends_at"],
                "time_zone": "America/Bogota",
                "reasons": ["SAME_CITY"],
            },
        )
        evaluated_at = datetime.fromisoformat(body["evaluated_at"])
        self.assertEqual(evaluated_at.utcoffset(), timedelta(0))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        sources = [dict(r.url.params) for r in self.events.requests if r.url.path == "/v1/events"]
        period = {"from": self.own["starts_at"], "to": self.own["ends_at"], "limit": "200"}
        self.assertEqual(
            sources, [{**period, "discipline": "Running"}, {**period, "city": "Bogotá"}]
        )

    def test_own_published_event_is_excluded(self) -> None:
        self.own["status"] = "PUBLISHED"
        self.events.published = [self.own]

        response = self.conflicts()

        self.assertEqual(response.json()["conflicts"], [])

    def test_own_event_found_on_a_later_page(self) -> None:
        """El evento propio se busca en `GET /v1/me/events` página a página (límite 100)."""
        self.events.mine = [event(START, status="DRAFT") for _ in range(150)] + [self.own]
        self.events.published = [event(START, city="Bogotá", discipline="Ciclismo")]

        response = self.conflicts()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["conflicts"]), 1)
        mine = [dict(r.url.params) for r in self.events.requests if r.url.path == "/v1/me/events"]
        self.assertEqual(mine, [{"limit": "100"}, {"limit": "100", "cursor": "100"}])

    def test_foreign_or_missing_event_is_not_found(self) -> None:
        """Un evento de otro organizador, aunque esté publicado, responde `404` sin consultar."""
        foreign = event(START)
        self.events.published = [foreign]
        for event_id in (foreign["event_id"], str(uuid.uuid4())):
            with self.subTest(event_id=event_id):
                response = self.conflicts(event_id)
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (404, "event_not_found")
                )
        self.assertNotIn("/v1/events", self.events.paths())

    def test_conflicts_follow_every_source_page(self) -> None:
        self.events.published = [
            event(START + timedelta(minutes=i % 60), discipline="Ciclismo") for i in range(250)
        ]

        response = self.conflicts()

        self.assertEqual(len(response.json()["conflicts"]), 250)
        cursors = [
            r.url.params.get("cursor") for r in self.events.requests if r.url.path == "/v1/events"
        ]
        self.assertEqual(cursors, [None, None, "200"])

    def test_conflicts_upstream_failures(self) -> None:
        """Fuente caída → `503`; sin respuesta → `504`; `403` del servicio sin alterar."""
        for failure, status in ((httpx.ConnectError, 503), (httpx.ReadTimeout, 504)):
            with self.subTest(failure=failure.__name__):
                self.events.failure = failure
                self.assertEqual(self.conflicts().status_code, status)
        self.events.failure = None
        for path, upstream, expected in (
            ("/v1/events", 500, 503),
            ("/v1/events", 422, 503),
            ("/v1/events", 403, 403),
            ("/v1/me/events", 403, 403),
            ("/v1/me/events", 500, 503),
        ):
            with self.subTest(path=path, upstream=upstream):
                self.events.status = {path: upstream}
                self.assertEqual(self.conflicts().status_code, expected)


if __name__ == "__main__":
    unittest.main()
