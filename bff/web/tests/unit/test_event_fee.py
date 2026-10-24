"""BFF web, HU025: filtro de borde `events.pricing.manage`, reenvío de cuerpo, versión y clave
tal cual, y errores del servicio sin alterar (ADR-040)."""

import json
import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_training import Upstreams, token

from bff_web.main import create_app
from bff_web.settings import BffWebSettings

EVENT_ID = str(uuid.uuid4())
OWNED = {
    "event_id": EVENT_ID,
    "version": 3,
    "fee": {"amount": "85000.00", "currency": "COP"},
    "registration_count": 1,
}
BODY = {"amount": "85000.00", "currency": "COP", "expected_version": 2}


def error(code: str, details: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {"code": code, "message": "m", "correlation_id": None, "details": details or []}


class EventFeeRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstreams = Upstreams()
        self.requests: list[httpx.Request] = []
        self.response = httpx.Response(200, json=OWNED, headers={"Idempotent-Replayed": "true"})
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

    def auth(self, actor: str | None = "organizer", **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(actor)}", **extra}

    def put(self, body: Any = BODY, actor: str | None = "organizer", **headers: str) -> Any:
        return self.client.put(
            f"/v1/events/{EVENT_ID}/fee", json=body, headers=self.auth(actor, **headers)
        )

    def delete(
        self, params: dict[str, str], actor: str | None = "organizer", **headers: str
    ) -> Any:
        return self.client.delete(
            f"/v1/events/{EVENT_ID}/fee", params=params, headers=self.auth(actor, **headers)
        )

    def test_put_forwards_body_key_and_token(self) -> None:
        """CA1, CA4 y CA5: el cuerpo y la clave llegan tal cual; la respuesta vuelve igual."""
        key = str(uuid.uuid4())
        raw = {"amount": "1.234", "currency": "USD", "expected_version": "x", "extra": True}

        response = self.put(**{"Idempotency-Key": key})
        self.put(raw)

        self.assertEqual((response.status_code, response.json()), (200, OWNED))
        self.assertEqual(response.headers["Idempotent-Replayed"], "true")
        first, second = self.requests
        self.assertEqual((first.method, first.url.path), ("PUT", f"/v1/events/{EVENT_ID}/fee"))
        self.assertEqual(json.loads(first.content), BODY)
        self.assertEqual(first.headers["Idempotency-Key"], key)
        self.assertTrue(first.headers["Authorization"].startswith("Bearer "))
        # El BFF no valida reglas del dominio: el servicio decide.
        self.assertEqual(json.loads(second.content), raw)
        self.assertNotIn("Idempotency-Key", second.headers)

    def test_delete_forwards_expected_version_and_key(self) -> None:
        key = str(uuid.uuid4())

        response = self.delete({"expected_version": "3"}, **{"Idempotency-Key": key})
        self.delete({})

        self.assertEqual(response.status_code, 200)
        first, second = self.requests
        self.assertEqual(
            (first.method, dict(first.url.params)), ("DELETE", {"expected_version": "3"})
        )
        self.assertEqual(first.headers["Idempotency-Key"], key)
        self.assertEqual(dict(second.url.params), {})

    def test_edge_filter(self) -> None:
        """Solo `organizer` con `events.pricing.manage`; los demás no llegan al servicio."""
        for actor in ("athlete", "service_provider", None):
            with self.subTest(actor=actor):
                for response in (self.put(actor=actor), self.delete({}, actor=actor)):
                    self.assertEqual(
                        (response.status_code, response.json()["code"]),
                        (403, "permission_required"),
                    )
        anonymous = self.client.put(f"/v1/events/{EVENT_ID}/fee", json=BODY)
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(self.requests, [])

    def test_service_errors_are_forwarded_and_failures_normalized(self) -> None:
        """CA2 y CA3: `422` con `details`, `404` y `409` llegan sin alterar."""
        cases = {
            404: error("event_not_found"),
            409: error("version_conflict", [{"field": "expected_version", "issue": "4"}]),
            422: error(
                "validation_error", [{"field": "currency", "issue": "unsupported_currency"}]
            ),
        }
        for status, body in cases.items():
            with self.subTest(status=status):
                self.response = httpx.Response(status, json=body)
                for response in (self.put(), self.delete({"expected_version": "1"})):
                    self.assertEqual((response.status_code, response.json()), (status, body))
        self.response = httpx.Response(500)
        self.assertEqual(self.put().json()["code"], "upstream_unavailable")
        self.failure = httpx.ReadTimeout
        self.assertEqual(self.put().status_code, 504)
        self.failure = httpx.ConnectError
        self.assertEqual(self.delete({}).status_code, 503)


if __name__ == "__main__":
    unittest.main()
