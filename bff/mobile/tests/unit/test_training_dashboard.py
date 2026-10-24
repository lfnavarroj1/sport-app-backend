"""BFF móvil, HU033: tablero de entrenamientos (ADR-043).

El borde exige `athlete` y `training.dashboard.view`; `from`/`to` llegan al servicio tal como
los envió el móvil (con desfase) y la respuesta y los errores vuelven sin alterar.
"""

import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_capabilities import FREE_CAPABILITIES, Upstream, token

from bff_mobile.main import create_app
from bff_mobile.settings import BffMobileSettings

DASHBOARD = "/v1/me/training-dashboard"
BODY: dict[str, Any] = {
    "period": {"from": "2026-10-01T00:00:00-05:00", "to": "2026-11-01T00:00:00-05:00"},
    "has_data": True,
    "totals": {
        "sessions_count": 2,
        "total_duration_minutes": 75,
        "avg_duration_minutes": 38,
        "distance_meters": None,
    },
    "by_sport": [
        {
            "sport": "running",
            "sessions_count": 2,
            "total_duration_minutes": 75,
            "distance_meters": None,
        }
    ],
    "by_week": [{"week_start": "2026-10-05", "sessions_count": 2, "total_duration_minutes": 75}],
    "by_source": {
        "SPORTAPP": {"sessions_count": 2, "total_duration_minutes": 75},
        "STRAVA": {"sessions_count": 0, "total_duration_minutes": 0},
    },
    "latest_metrics": [
        {
            "indicator": "FTP_ESTIMATED",
            "value": 238,
            "unit": "W",
            "method_code": "FTP_20_MIN_95_V1",
            "method_version": 1,
            "measured_at": "2026-10-15T12:00:00Z",
            "disclaimer_key": "estimated_not_diagnostic",
        }
    ],
    "sources": [
        {"source": "training_sessions", "status": "OK"},
        {"source": "training_metrics", "status": "OK"},
    ],
}


def error(code: str, details: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {"code": code, "message": "m", "correlation_id": None, "details": details or []}


class TrainingDashboardRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = Upstream(
            {"subscription_status": "ACTIVE", "capabilities": FREE_CAPABILITIES}
        )
        self.reply: httpx.Response = httpx.Response(200, json=BODY)
        self.failure: type[Exception] | None = None
        self.requests: list[httpx.Request] = []

        def training(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.failure is not None:
                raise self.failure("entrenamientos")
            return self.reply

        self.client = TestClient(
            create_app(
                BffMobileSettings(
                    app_env="test",  # type: ignore[arg-type]
                    users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
                    affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
                    training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
                    events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                ),
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
                    base_url="http://events",
                    transport=httpx.MockTransport(lambda _: httpx.Response(500)),
                ),
            )
        )

    def get(
        self, params: dict[str, str] | None = None, actor: str | None = "athlete", **headers: str
    ) -> httpx.Response:
        sent = dict(headers)
        if actor is not None:
            sent["Authorization"] = f"Bearer {token(actor)}"
        return self.client.get(DASHBOARD, params=params, headers=sent)

    def test_forwards_period_token_and_correlation_and_relays_the_body(self) -> None:
        """CA1/CA3: el periodo con desfase llega tal cual; el cuerpo vuelve sin alterar."""
        correlation = str(uuid.uuid4())
        params = {"from": "2026-10-01T00:00:00-05:00", "to": "2026-11-01T00:00:00+09:00"}

        response = self.get(params, **{"X-Correlation-ID": correlation})

        self.assertEqual((response.status_code, response.json()), (200, BODY))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        [sent] = self.requests
        self.assertEqual((sent.method, sent.url.path), ("GET", DASHBOARD))
        self.assertEqual(dict(sent.url.params), params)
        self.assertEqual(sent.headers["X-Correlation-ID"], correlation)
        self.assertTrue(sent.headers["Authorization"].startswith("Bearer "))
        self.assertIn("/capabilities", self.upstream.affiliation_paths[0])

    def test_without_period_nothing_is_added(self) -> None:
        """Sin `from`/`to` el servicio aplica los últimos 30 días."""
        self.get()

        self.assertEqual(dict(self.requests[0].url.params), {})

    def test_edge_rejects_other_actors_missing_token_and_capability(self) -> None:
        """CA4: otro actor → `403 permission_required`; sin capacidad → `capability_required`."""
        for actor in ("organizer", "service_provider"):
            with self.subTest(actor=actor):
                response = self.get(actor=actor)
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )
        anonymous = self.get(actor=None)
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(anonymous.headers["Cache-Control"], "no-store")
        self.upstream.snapshot = {
            "subscription_status": "ACTIVE",
            "capabilities": ["training.plan.manage"],
        }
        without = self.get()
        self.assertEqual(
            (without.status_code, without.json()["code"]), (403, "capability_required")
        )
        self.upstream.snapshot = {"subscription_status": "PROVISIONING", "capabilities": []}
        provisioning = self.get()
        self.assertEqual(provisioning.json()["code"], "capability_required")
        self.assertEqual(self.requests, [])

    def test_affiliation_failures(self) -> None:
        self.upstream.status = 0

        response = self.get()

        self.assertEqual(
            (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
        )
        self.assertEqual(self.requests, [])

    def test_service_errors_pass_through_unchanged(self) -> None:
        """CA4: `422 validation_error` con `details`, `403` y `503`/`504` del servicio."""
        for status, payload in (
            (422, error("validation_error", [{"field": "query.to", "issue": "out_of_range"}])),
            (403, error("capability_required")),
            (503, error("upstream_unavailable")),
            (504, error("upstream_timeout")),
        ):
            with self.subTest(status=status):
                self.reply = httpx.Response(status, json=payload)
                response = self.get({"from": "2025-01-01T00:00:00Z", "to": "2026-06-01T00:00:00Z"})
                self.assertEqual((response.status_code, response.json()), (status, payload))
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_unexpected_status_and_transport_failures(self) -> None:
        self.reply = httpx.Response(500, json=error("boom"))
        self.assertEqual(self.get().status_code, 503)
        for failure, status in ((httpx.ConnectError, 503), (httpx.ReadTimeout, 504)):
            with self.subTest(failure=failure.__name__):
                self.failure = failure
                response = self.get()
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main()
