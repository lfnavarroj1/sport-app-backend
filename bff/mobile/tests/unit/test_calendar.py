"""BFF móvil, HU035: composición del calendario del deportista (ADR-031)."""

import unittest
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_capabilities import FREE_CAPABILITIES, Upstream, token

from bff_mobile.api.calendar_composition import (
    InvalidPeriod,
    event_item,
    normalize_period,
    ordered,
    session_item,
)
from bff_mobile.main import create_app
from bff_mobile.settings import BffMobileSettings

NOW = datetime(2026, 10, 12, 22, 0, tzinfo=UTC)
PERIOD = "from=2026-11-01T00:00:00-05:00&to=2026-11-30T23:59:59-05:00"


def published(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "name": "Carrera 10K Bogotá",
        "discipline": "Running",
        "location": {
            "name": "Parque Metropolitano Simón Bolívar",
            "city": "Bogotá",
            "country_code": "CO",
        },
        "starts_at": "2026-11-15T12:00:00Z",
        "ends_at": "2026-11-15T15:00:00Z",
        "time_zone": "America/Bogota",
        "status": "PUBLISHED",
        "version": 2,
    }
    values.update(overrides)
    return values


def planned(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "plan_id": str(uuid.uuid4()),
        "plan_title": "Base aeróbica noviembre",
        "session_id": str(uuid.uuid4()),
        "scheduled_date": "2026-11-16",
        "sport": "running",
        "duration_minutes": 45,
        "rpe": 4,
    }
    values.update(overrides)
    return values


class PeriodTest(unittest.TestCase):
    def test_default_is_the_current_utc_month(self) -> None:
        period = normalize_period(None, None, NOW)

        self.assertEqual(period.period_from, datetime(2026, 10, 1, tzinfo=UTC))
        self.assertEqual(period.period_to, datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC))

    def test_dates_for_sessions_use_the_device_offset(self) -> None:
        period = normalize_period("2026-11-01T00:00:00-05:00", "2026-11-30T23:59:59-05:00", NOW)

        self.assertEqual(
            (period.date_from.isoformat(), period.date_to.isoformat()), ("2026-11-01", "2026-11-30")
        )

    def test_invalid_periods(self) -> None:
        """CA6: causas estables."""
        for raw_from, raw_to, field, issue in (
            ("2026-11-01T00:00:00", "2026-11-30T00:00:00Z", "from", "invalid_datetime"),
            ("ayer", "2026-11-30T00:00:00Z", "from", "invalid_datetime"),
            ("2026-11-30T00:00:00Z", "2026-11-01T00:00:00Z", "to", "ends_before_starts"),
            ("2026-11-01T00:00:00Z", "2027-02-01T00:00:01Z", "to", "out_of_range"),
            ("2026-11-01T00:00:00Z", None, "to", "required"),
        ):
            with self.subTest(issue=issue, field=field), self.assertRaises(InvalidPeriod) as raised:
                normalize_period(raw_from, raw_to, NOW)
            self.assertEqual((raised.exception.field, raised.exception.issue), (field, issue))
        self.assertEqual(
            normalize_period(
                "2026-11-01T00:00:00Z", "2027-02-01T00:00:00Z", NOW
            ).date_to.isoformat(),
            "2027-02-01",
            "92 días exactos es válido",
        )


class CompositionTest(unittest.TestCase):
    def test_occurs_on_uses_the_event_time_zone(self) -> None:
        """23:30 del 14 en Bogotá es 04:30Z del 15: el evento ocurre el 14 (CA4)."""
        item = event_item(
            published(starts_at="2026-11-15T04:30:00Z", ends_at="2026-11-15T06:00:00Z")
        )

        self.assertEqual(item["occurs_on"], "2026-11-14")
        self.assertEqual(
            (item["starts_at"], item["time_zone"]), ("2026-11-15T04:30:00Z", "America/Bogota")
        )

    def test_order_sessions_first_then_by_time(self) -> None:
        late = event_item(
            published(starts_at="2026-11-16T20:00:00Z", ends_at="2026-11-16T21:00:00Z")
        )
        early = event_item(
            published(starts_at="2026-11-16T12:00:00Z", ends_at="2026-11-16T13:00:00Z")
        )
        session = session_item(planned(scheduled_date="2026-11-16"))
        previous_day = session_item(planned(scheduled_date="2026-11-15"))

        result = ordered([late, early, session, previous_day])

        self.assertEqual(result, [previous_day, session, early, late])


class CalendarRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = Upstream(
            {"subscription_status": "ACTIVE", "capabilities": FREE_CAPABILITIES}
        )
        self.events: list[dict[str, Any]] = [published()]
        self.sessions: list[dict[str, Any]] = [planned()]
        self.events_failure: type[Exception] | None = None
        self.training_failure: type[Exception] | None = None
        self.events_next: str | None = None
        self.requests: list[httpx.Request] = []

        def events(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.events_failure is not None:
                raise self.events_failure("eventos")
            return httpx.Response(200, json={"items": self.events, "next_cursor": self.events_next})

        def training(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.training_failure is not None:
                raise self.training_failure("entrenamientos")
            return httpx.Response(200, json={"items": self.sessions, "next_cursor": None})

        settings = BffMobileSettings(
            app_env="test",  # type: ignore[arg-type]
            users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
            affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
            training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
            events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
            complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
            third_party_management_base_url="http://third-party.invalid",  # type: ignore[arg-type]
        )
        self.client = TestClient(
            create_app(
                settings,
                users_http=httpx.Client(
                    base_url="http://users", transport=httpx.MockTransport(self.upstream.users)
                ),
                affiliation_http=httpx.Client(
                    base_url="http://affiliation",
                    transport=httpx.MockTransport(self.upstream.affiliation),
                ),
                training_http=httpx.Client(
                    base_url="http://training", transport=httpx.MockTransport(training)
                ),
                events_http=httpx.Client(
                    base_url="http://events", transport=httpx.MockTransport(events)
                ),
            )
        )

    def get(self, query: str = PERIOD, actor: str = "athlete", **headers: str) -> Any:
        return self.client.get(
            f"/v1/me/calendar?{query}",
            headers={"Authorization": f"Bearer {token(actor)}", **headers},
        )

    def test_composes_both_sources_like_the_adr_example(self) -> None:
        """CA1: ambas fuentes, orden cronológico y fuente identificada."""
        correlation = str(uuid.uuid4())
        response = self.get(**{"X-Correlation-ID": correlation})

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(
            (body["from"], body["to"]), ("2026-11-01T05:00:00Z", "2026-12-01T04:59:59Z")
        )
        self.assertEqual(
            [item["source"] for item in body["items"]], ["EVENT_PUBLISHED", "TRAINING_PLAN_SESSION"]
        )
        event, session = body["items"]
        self.assertEqual(event["item_id"], f"EVENT_PUBLISHED:{self.events[0]['event_id']}")
        self.assertEqual(event["reference"], {"event_id": self.events[0]["event_id"]})
        self.assertEqual(
            {
                key: session[key]
                for key in ("starts_at", "ends_at", "time_zone", "occurs_on", "rpe")
            },
            {
                "starts_at": None,
                "ends_at": None,
                "time_zone": None,
                "occurs_on": "2026-11-16",
                "rpe": 4,
            },
        )
        self.assertEqual(
            body["sources"],
            [
                {"name": "events", "status": "OK", "truncated": False},
                {"name": "training", "status": "OK", "truncated": False},
            ],
        )
        self.assertNotIn("athlete_id", response.text)
        events_request = next(r for r in self.requests if r.url.path == "/v1/events")
        training_request = next(
            r for r in self.requests if r.url.path == "/v1/me/training-plan-sessions"
        )
        self.assertEqual(events_request.url.params["limit"], "200")
        self.assertEqual(
            (training_request.url.params["from"], training_request.url.params["to"]),
            ("2026-11-01", "2026-11-30"),
        )
        self.assertEqual(events_request.headers["X-Correlation-ID"], correlation)
        self.assertEqual(training_request.headers["X-Correlation-ID"], correlation)

    def test_empty_calendar_is_not_an_error(self) -> None:
        """CA2."""
        self.events, self.sessions = [], []

        response = self.get()

        self.assertEqual((response.status_code, response.json()["items"]), (200, []))

    def test_one_source_down_returns_partial_result(self) -> None:
        """CA7: la otra fuente y la incompleta declarada."""
        self.events_failure = httpx.ConnectError
        down = self.get()
        self.events_failure, self.training_failure = None, httpx.ReadTimeout
        slow = self.get()

        self.assertEqual(down.status_code, 200)
        self.assertEqual(
            [item["source"] for item in down.json()["items"]], ["TRAINING_PLAN_SESSION"]
        )
        self.assertEqual(
            down.json()["sources"][0],
            {"name": "events", "status": "UNAVAILABLE", "truncated": False},
        )
        self.assertEqual(
            slow.json()["sources"][1], {"name": "training", "status": "TIMEOUT", "truncated": False}
        )

    def test_both_sources_down(self) -> None:
        self.events_failure = self.training_failure = httpx.ReadTimeout
        timeout = self.get()
        self.events_failure = httpx.ConnectError
        mixed = self.get()

        self.assertEqual((timeout.status_code, timeout.json()["code"]), (504, "upstream_timeout"))
        self.assertEqual((mixed.status_code, mixed.json()["code"]), (503, "upstream_unavailable"))

    def test_truncated_source(self) -> None:
        self.events_next = "cursor"

        response = self.get()

        self.assertTrue(response.json()["sources"][0]["truncated"])

    def test_actor_capability_and_period(self) -> None:
        """CA5 y CA6."""
        organizer = self.get(actor="organizer")
        invalid = self.get("from=2026-11-30T00:00:00Z&to=2026-11-01T00:00:00Z")
        self.upstream.snapshot = {"subscription_status": "NONE", "capabilities": []}
        no_capability = self.get()
        self.upstream.status = 0
        affiliation_down = self.get()
        anonymous = self.client.get("/v1/me/calendar")

        self.assertEqual(
            (organizer.status_code, organizer.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(
            (invalid.status_code, invalid.json()["details"]),
            (422, [{"field": "query.to", "issue": "ends_before_starts"}]),
        )
        self.assertEqual(
            (no_capability.status_code, no_capability.json()["code"]), (403, "capability_required")
        )
        self.assertEqual(affiliation_down.status_code, 503)
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(self.requests, [], "ningún rechazo consulta las fuentes")


if __name__ == "__main__":
    unittest.main()
