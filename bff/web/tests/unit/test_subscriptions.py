"""BFF web, HU016: filtro de borde `athlete`, reenvío de token, cuerpo y clave a Afiliación,
errores sin alterar, `no-store` y fallas del transporte (ADR-036)."""

import json
import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_training import Upstreams, token

from bff_web.main import create_app
from bff_web.settings import BffWebSettings

SUBSCRIPTION = {
    "subscription_id": str(uuid.uuid4()),
    "plan_code": "PREMIUM",
    "plan_version": 2,
    "periodicity": "QUARTERLY",
    "price": "14400.00",
    "period_start": "2026-10-23T15:00:00Z",
    "period_end": "2027-01-23T15:00:00Z",
    "status": "ACTIVE",
    "origin": "USER_CHANGE",
    "version": 2,
}
BODY = {"plan_code": "PREMIUM", "periodicity": "QUARTERLY", "expected_subscription_version": 1}


def error(code: str) -> dict[str, Any]:
    return {"code": code, "message": "m", "correlation_id": None, "details": []}


class SubscriptionRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.requests: list[httpx.Request] = []
        self.response = httpx.Response(200, json=SUBSCRIPTION)
        self.failure: type[Exception] | None = None

        def affiliation(request: httpx.Request) -> httpx.Response:
            if self.failure is not None:
                raise self.failure("afiliación")
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
                    transport=httpx.MockTransport(Upstreams().users), base_url="http://users"
                ),
                affiliation_http=httpx.Client(
                    transport=httpx.MockTransport(affiliation), base_url="http://affiliation"
                ),
            )
        )

    def auth(self, actor: str | None = "athlete", **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(actor)}", **extra}

    def change(
        self, body: Any = BODY, actor: str | None = "athlete", key: str | None = "new"
    ) -> Any:
        headers = self.auth(actor)
        if key is not None:
            headers["Idempotency-Key"] = str(uuid.uuid4()) if key == "new" else key
        return self.client.post("/v1/me/subscription/changes", json=body, headers=headers)

    def test_queries_forward_the_token_and_disable_cache(self) -> None:
        for path, payload in (
            ("/v1/plans", {"catalog_version": "c", "current_plan_code": "FREE", "plans": []}),
            ("/v1/me/subscription", SUBSCRIPTION),
        ):
            with self.subTest(path=path):
                self.response = httpx.Response(200, json=payload)
                response = self.client.get(path, headers=self.auth())
                self.assertEqual((response.status_code, response.json()), (200, payload))
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                sent = self.requests[-1]
                self.assertEqual(sent.url.path, path)
                self.assertTrue(sent.headers["Authorization"].startswith("Bearer "))

    def test_change_forwards_body_key_and_replay_header(self) -> None:
        key = str(uuid.uuid4())
        self.response = httpx.Response(
            200, json=SUBSCRIPTION, headers={"Idempotent-Replayed": "true"}
        )

        response = self.change(key=key)

        self.assertEqual((response.status_code, response.json()), (200, SUBSCRIPTION))
        self.assertEqual(response.headers["Idempotent-Replayed"], "true")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        [sent] = self.requests
        self.assertEqual((sent.method, sent.url.path), ("POST", "/v1/me/subscription/changes"))
        self.assertEqual(json.loads(sent.content), BODY)
        self.assertEqual(sent.headers["Idempotency-Key"], key)

    def test_edge_filter_only_athletes(self) -> None:
        for actor in ("organizer", "service_provider", None):
            with self.subTest(actor=actor):
                for response in (
                    self.client.get("/v1/plans", headers=self.auth(actor)),
                    self.client.get("/v1/me/subscription", headers=self.auth(actor)),
                    self.change(actor=actor),
                ):
                    self.assertEqual(
                        (response.status_code, response.json()["code"]),
                        (403, "permission_required"),
                    )
        self.assertEqual(self.client.get("/v1/plans").status_code, 401)
        self.assertEqual(self.requests, [])

    def test_service_errors_pass_through_unchanged(self) -> None:
        for status, code in (
            (404, "plan_not_found"),
            (409, "plan_not_selectable"),
            (409, "subscription_unchanged"),
            (409, "subscription_provisioning"),
            (409, "version_conflict"),
            (409, "idempotency_key_reused"),
            (422, "validation_error"),
            (403, "permission_required"),
        ):
            with self.subTest(code=code):
                self.response = httpx.Response(status, json=error(code))
                response = self.change()
                self.assertEqual((response.status_code, response.json()["code"]), (status, code))

    def test_shape_errors_are_rejected_at_the_edge(self) -> None:
        missing_key = self.change(key=None)
        bad_body = self.change(body={**BODY, "periodicity": "WEEKLY"})

        for response in (missing_key, bad_body):
            self.assertEqual(
                (response.status_code, response.json()["code"]), (422, "validation_error")
            )
        self.assertEqual(self.requests, [])

    def test_unexpected_status_and_transport_failures(self) -> None:
        self.response = httpx.Response(500, json=error("boom"))
        self.assertEqual(self.change().status_code, 503)
        for failure, status in ((httpx.ConnectError, 503), (httpx.ReadTimeout, 504)):
            with self.subTest(failure=failure.__name__):
                self.failure = failure
                self.assertEqual(self.change().status_code, status)
                self.assertEqual(
                    self.client.get("/v1/plans", headers=self.auth()).status_code, status
                )


if __name__ == "__main__":
    unittest.main()
