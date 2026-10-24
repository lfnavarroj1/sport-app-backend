"""BFF móvil, HU032: filtro de borde por actor, reenvío literal y errores del servicio."""

import json
import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_capabilities import Upstream, token

from bff_mobile.main import create_app
from bff_mobile.settings import BffMobileSettings

BODY = {
    "session_id": str(uuid.uuid4()),
    "plan_id": None,
    "sport": "running",
    "started_at": "2026-10-12T12:30:00Z",
}


class TrainingSessionsRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = Upstream()
        self.requests: list[httpx.Request] = []
        self.response = httpx.Response(
            201,
            json={"session_id": BODY["session_id"]},
            headers={
                "Location": f"/v1/training-sessions/{BODY['session_id']}",
                "Idempotent-Replayed": "true",
            },
        )
        self.failure: type[Exception] | None = None

        def training(request: httpx.Request) -> httpx.Response:
            if self.failure is not None:
                raise self.failure("entrenamientos")
            self.requests.append(request)
            return self.response

        settings = BffMobileSettings(
            app_env="test",  # type: ignore[arg-type]
            users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
            affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
            training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
            events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
            complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
        )
        self.client = TestClient(
            create_app(
                settings,
                users_http=httpx.Client(
                    base_url="http://users", transport=httpx.MockTransport(self.upstream.users)
                ),
                training_http=httpx.Client(
                    base_url="http://training", transport=httpx.MockTransport(training)
                ),
            )
        )

    def headers(self, actor: str | None = "athlete", **extra: str) -> dict[str, str]:
        sent = dict(extra)
        if actor is not None:
            sent["Authorization"] = f"Bearer {token(actor)}"
        return sent

    def test_start_forwards_body_key_and_correlation_verbatim(self) -> None:
        key, correlation = str(uuid.uuid4()), str(uuid.uuid4())
        body = {**BODY, "athlete_id": "inyectado"}

        response = self.client.post(
            "/v1/training-sessions",
            json=body,
            headers=self.headers(**{"Idempotency-Key": key, "X-Correlation-ID": correlation}),
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.headers["Location"], f"/v1/training-sessions/{BODY['session_id']}"
        )
        self.assertEqual(response.headers["Idempotent-Replayed"], "true")
        [sent] = self.requests
        self.assertEqual(sent.url.path, "/v1/training-sessions")
        self.assertEqual(json.loads(sent.content), body)
        self.assertEqual(sent.headers["Idempotency-Key"], key)
        self.assertEqual(sent.headers["X-Correlation-ID"], correlation)
        self.assertTrue(sent.headers["Authorization"].startswith("Bearer "))

    def test_finish_and_active_routes(self) -> None:
        session_id = str(uuid.uuid4())
        self.response = httpx.Response(200, json={"status": "FINISHED"})
        finished = self.client.post(
            f"/v1/training-sessions/{session_id}/finish",
            json={"ended_at": "2026-10-12T13:15:00Z"},
            headers=self.headers(**{"Idempotency-Key": str(uuid.uuid4())}),
        )
        self.response = httpx.Response(204)
        empty = self.client.get("/v1/training-sessions/active", headers=self.headers())

        self.assertEqual((finished.status_code, finished.json()), (200, {"status": "FINISHED"}))
        self.assertEqual(empty.status_code, 204)
        self.assertEqual(
            [request.url.path for request in self.requests],
            [f"/v1/training-sessions/{session_id}/finish", "/v1/training-sessions/active"],
        )

    def test_edge_filter_by_actor(self) -> None:
        """Solo `athlete`; el resto no llega al servicio."""
        for actor in ("organizer", "service_provider"):
            with self.subTest(actor=actor):
                response = self.client.post(
                    "/v1/training-sessions", json=BODY, headers=self.headers(actor)
                )
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )
        anonymous = self.client.get("/v1/training-sessions/active")
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(self.requests, [])

    def test_service_errors_pass_through(self) -> None:
        for status, code in (
            (403, "capability_required"),
            (404, "training_session_not_found"),
            (409, "active_session_exists"),
            (422, "validation_error"),
            (503, "upstream_unavailable"),
            (504, "upstream_timeout"),
        ):
            payload: dict[str, Any] = {
                "code": code,
                "message": "m",
                "correlation_id": None,
                "details": [],
            }
            with self.subTest(code=code):
                self.response = httpx.Response(status, json=payload)
                response = self.client.post(
                    "/v1/training-sessions", json=BODY, headers=self.headers()
                )
                self.assertEqual((response.status_code, response.json()), (status, payload))

    def test_failures_are_retryable_statuses(self) -> None:
        """La app solo reintenta ante red, 502, 503 o 504 (ADR-026, ADR-029)."""
        self.failure = httpx.ReadTimeout
        timeout = self.client.post("/v1/training-sessions", json=BODY, headers=self.headers())
        self.failure = httpx.ConnectError
        down = self.client.post("/v1/training-sessions", json=BODY, headers=self.headers())
        self.failure = None
        self.response = httpx.Response(500)
        with self.assertLogs("sportapp.bff", level="WARNING"):
            unexpected = self.client.get("/v1/training-sessions/active", headers=self.headers())

        self.assertEqual((timeout.status_code, timeout.json()["code"]), (504, "upstream_timeout"))
        self.assertEqual(down.status_code, 503)
        self.assertEqual(unexpected.status_code, 503)


if __name__ == "__main__":
    unittest.main()
