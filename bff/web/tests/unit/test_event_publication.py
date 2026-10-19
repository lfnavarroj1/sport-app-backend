"""BFF web, HU024: filtro de borde, reenvío de la clave y del filtro, errores del servicio sin
alterar y detalle sin `my_registration` (ADR-033)."""

import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_training import Upstreams, token

from bff_web.main import create_app
from bff_web.settings import BffWebSettings

AGGREGATE = {"event_id": str(uuid.uuid4()), "status": "PUBLISHED", "version": 2}


def error(code: str) -> dict[str, Any]:
    return {"code": code, "message": "m", "correlation_id": None, "details": []}


class EventPublicationRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstreams = Upstreams()
        self.requests: list[httpx.Request] = []
        self.response = httpx.Response(200, json=AGGREGATE, headers={"Idempotent-Replayed": "true"})
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

    def publish(self, actor: str | None = "organizer", **headers: str) -> Any:
        return self.client.post(
            f"/v1/events/{AGGREGATE['event_id']}/publish", headers=self.auth(actor, **headers)
        )

    def test_publish_forwards_key_and_token_without_body(self) -> None:
        key = str(uuid.uuid4())

        response = self.publish(**{"Idempotency-Key": key})

        self.assertEqual((response.status_code, response.json()), (200, AGGREGATE))
        self.assertEqual(response.headers["Idempotent-Replayed"], "true")
        [sent] = self.requests
        self.assertEqual(
            (sent.method, sent.url.path), ("POST", f"/v1/events/{AGGREGATE['event_id']}/publish")
        )
        self.assertEqual(sent.content, b"")
        self.assertEqual(sent.headers["Idempotency-Key"], key)
        self.assertTrue(sent.headers["Authorization"].startswith("Bearer "))

    def test_publish_edge_filter(self) -> None:
        """CA6: solo `organizer` con `events.publish`; los demás no llegan al servicio."""
        for actor in ("athlete", "service_provider", None):
            with self.subTest(actor=actor):
                response = self.publish(actor)
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )
        anonymous = self.client.post(f"/v1/events/{AGGREGATE['event_id']}/publish")
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(self.requests, [])

    def test_service_errors_are_forwarded_and_failures_normalized(self) -> None:
        """CA2, CA3 y CA4: `422` con `details`, `404` y `409` llegan sin alterar."""
        invalid = {
            **error("validation_error"),
            "details": [{"field": "starts_at", "issue": "not_in_future"}],
        }
        cases = {
            404: error("event_not_found"),
            409: error("idempotency_key_reused"),
            422: invalid,
        }
        for status, body in cases.items():
            with self.subTest(status=status):
                self.response = httpx.Response(status, json=body)
                response = self.publish(**{"Idempotency-Key": str(uuid.uuid4())})
                self.assertEqual((response.status_code, response.json()), (status, body))
        self.response = httpx.Response(500)
        self.assertEqual(self.publish().json()["code"], "upstream_unavailable")
        self.failure = httpx.ReadTimeout
        self.assertEqual(self.publish().status_code, 504)
        self.failure = httpx.ConnectError
        self.assertEqual(self.publish().status_code, 503)

    def test_my_events_forwards_filters_only_for_organizers(self) -> None:
        """CA6: lista propia con `status`, `limit` y `cursor`; otro actor → `403`."""
        page = {"items": [AGGREGATE], "next_cursor": None}
        self.response = httpx.Response(200, json=page)

        response = self.client.get(
            "/v1/me/events",
            params={"status": "DRAFT", "limit": 5, "cursor": "abc"},
            headers=self.auth(),
        )
        default = self.client.get("/v1/me/events", headers=self.auth())
        athlete = self.client.get("/v1/me/events", headers=self.auth("athlete"))
        invalid = self.client.get("/v1/me/events", params={"status": "X"}, headers=self.auth())

        self.assertEqual((response.status_code, response.json()), (200, page))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(
            dict(self.requests[0].url.params), {"status": "DRAFT", "limit": "5", "cursor": "abc"}
        )
        self.assertEqual(dict(self.requests[1].url.params), {"limit": "20"})
        self.assertEqual(default.status_code, 200)
        self.assertEqual(
            (athlete.status_code, athlete.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(len(self.requests), 2)

    def test_detail_omits_my_registration_and_forwards_visibility(self) -> None:
        """ADR-033: el BFF web expone el detalle sin `my_registration`; el servicio decide si
        el `DRAFT` es visible (CA3)."""
        detail = {"event_id": AGGREGATE["event_id"], "status": "DRAFT", "my_registration": None}
        self.response = httpx.Response(200, json=detail)

        response = self.client.get(
            f"/v1/events/{AGGREGATE['event_id']}", headers=self.auth("service_provider")
        )
        self.response = httpx.Response(404, json=error("event_not_found"))
        hidden = self.client.get(f"/v1/events/{AGGREGATE['event_id']}", headers=self.auth())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"event_id": AGGREGATE["event_id"], "status": "DRAFT"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual((hidden.status_code, hidden.json()["code"]), (404, "event_not_found"))
        self.assertEqual(self.requests[0].url.path, f"/v1/events/{AGGREGATE['event_id']}")


if __name__ == "__main__":
    unittest.main()
