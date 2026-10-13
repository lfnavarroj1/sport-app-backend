"""BFF web, HU021: filtro de borde, reenvío literal del cuerpo y errores del servicio."""

import json
import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_training import Upstreams, token

from bff_web.main import create_app
from bff_web.settings import BffWebSettings

BODY = {
    "name": "Carrera 10K Bogotá",
    "description": "Carrera recreativa de diez kilómetros para corredores aficionados.",
    "discipline": "Running",
    "location": {
        "name": "Parque Metropolitano Simón Bolívar",
        "address": "Avenida Calle 63 # 60-50",
        "city": "Bogotá",
        "country_code": "CO",
    },
    "starts_at": "2026-11-15T07:00:00-05:00",
    "time_zone": "America/Bogota",
    "duration_minutes": 180,
    "capacity": 5000,
}


class EventsRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstreams = Upstreams()
        self.requests: list[httpx.Request] = []
        self.response = httpx.Response(
            201,
            json={"event_id": str(uuid.uuid4())},
            headers={"Location": "/v1/events/x", "Idempotent-Replayed": "true"},
        )
        self.failure: type[Exception] | None = None

        def events(request: httpx.Request) -> httpx.Response:
            if self.failure is not None:
                raise self.failure("eventos")
            self.requests.append(request)
            return self.response

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
                    transport=httpx.MockTransport(self.upstreams.users), base_url="http://users"
                ),
                events_http=httpx.Client(
                    transport=httpx.MockTransport(events), base_url="http://events"
                ),
            )
        )

    def post(self, body: Any, actor: str | None = "organizer", **headers: str) -> Any:
        sent = dict(headers)
        if actor is not None:
            sent["Authorization"] = f"Bearer {token(actor)}"
        return self.client.post("/v1/events", json=body, headers=sent)

    def test_forwards_body_key_and_token_verbatim(self) -> None:
        """El BFF no reinterpreta el evento: las reglas son del servicio (ADR-028)."""
        key, correlation = str(uuid.uuid4()), str(uuid.uuid4())
        body = {**BODY, "organizer_id": "inyectado"}

        response = self.post(body, **{"Idempotency-Key": key, "X-Correlation-ID": correlation})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers["Location"], "/v1/events/x")
        self.assertEqual(response.headers["Idempotent-Replayed"], "true")
        [sent] = self.requests
        self.assertEqual(sent.url.path, "/v1/events")
        self.assertEqual(json.loads(sent.content), body)
        self.assertEqual(sent.headers["Idempotency-Key"], key)
        self.assertEqual(sent.headers["X-Correlation-ID"], correlation)
        self.assertTrue(sent.headers["Authorization"].startswith("Bearer "))

    def test_missing_key_is_forwarded_for_the_service_to_report(self) -> None:
        self.post(BODY)

        self.assertNotIn("Idempotency-Key", self.requests[0].headers)

    def test_edge_permission(self) -> None:
        """CA2: solo `organizer` tiene `events.create` en web."""
        for actor in ("athlete", "service_provider"):
            with self.subTest(actor=actor):
                response = self.post(BODY, actor)
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )
        self.assertEqual(self.post(BODY, None).status_code, 401)
        self.assertEqual(self.requests, [])

    def test_service_errors_pass_through(self) -> None:
        detail = {"field": "organizer_id", "issue": "field_not_allowed"}
        for status, code, details in (
            (422, "validation_error", [detail]),
            (409, "idempotency_key_reused", []),
            (403, "permission_required", []),
        ):
            payload = {"code": code, "message": "m", "correlation_id": None, "details": details}
            with self.subTest(status=status):
                self.response = httpx.Response(status, json=payload)
                response = self.post(BODY)
                self.assertEqual((response.status_code, response.json()), (status, payload))

    def test_failures_are_retryable_statuses(self) -> None:
        """CA7: el cliente solo reintenta ante 502, 503 o 504; el BFF los produce así."""
        self.failure = httpx.ReadTimeout
        timeout = self.post(BODY)
        self.failure = httpx.ConnectError
        down = self.post(BODY)
        self.failure = None
        self.response = httpx.Response(500)
        with self.assertLogs("sportapp.bff", level="WARNING"):
            unexpected = self.post(BODY)

        self.assertEqual((timeout.status_code, timeout.json()["code"]), (504, "upstream_timeout"))
        self.assertEqual(down.status_code, 503)
        self.assertEqual(unexpected.status_code, 503)


if __name__ == "__main__":
    unittest.main()
