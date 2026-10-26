"""BFF web, HU007: borde para los tres actores, reenvío del token y de `limit`/`cursor` a
Afiliación, respuesta y errores sin alterar, `no-store` y fallas del transporte (ADR-036)."""

import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_training import Upstreams, token

from bff_web.main import create_app
from bff_web.settings import BffWebSettings

PATH = "/v1/me/account-statement"
STATEMENT = {
    "statement_status": "ACTIVE",
    "subscription": {
        "subscription_id": str(uuid.uuid4()),
        "plan_code": "INTERMEDIATE",
        "plan_version": 2,
        "periodicity": "MONTHLY",
        "price": "2400.00",
        "period_start": "2026-10-23T15:00:00Z",
        "period_end": "2026-11-23T15:00:00Z",
        "status": "ACTIVE",
        "origin": "USER_CHANGE",
        "version": 2,
    },
    "commitments": [
        {
            "commitment_id": str(uuid.uuid4()),
            "plan_code": "INTERMEDIATE",
            "periodicity": "MONTHLY",
            "amount": "2400.00",
            "currency": "COP",
            "period_start": "2026-10-23T15:00:00Z",
            "period_end": "2026-11-23T15:00:00Z",
            "due_at": "2026-10-23T15:00:00Z",
            "status": "PENDING_EXTERNAL",
        }
    ],
    "movements": [
        {
            "movement_id": str(uuid.uuid4()),
            "movement_type": "PLAN_CHANGED",
            "occurred_at": "2026-10-23T15:00:00Z",
            "subscription_id": str(uuid.uuid4()),
            "plan_code": "INTERMEDIATE",
            "periodicity": "MONTHLY",
            "commitment_id": None,
            "amount": None,
            "currency": None,
        }
    ],
    "next_cursor": "abc",
    "payments_status": "NOT_INTEGRATED",
    "balance": None,
}


def error(code: str, details: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {"code": code, "message": "m", "correlation_id": None, "details": details or []}


class AccountStatementRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.requests: list[httpx.Request] = []
        self.response = httpx.Response(200, json=STATEMENT)
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

    def get(self, actor: str | None = "athlete", **params: str) -> Any:
        return self.client.get(
            PATH, params=params, headers={"Authorization": f"Bearer {token(actor)}"}
        )

    def test_three_actors_reach_affiliation_with_their_token(self) -> None:
        for actor in ("athlete", "organizer", "service_provider"):
            with self.subTest(actor=actor):
                response = self.get(actor)
                self.assertEqual((response.status_code, response.json()), (200, STATEMENT))
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                sent = self.requests[-1]
                self.assertEqual((sent.method, sent.url.path), ("GET", PATH))
                self.assertEqual(sent.url.query, b"")
                self.assertTrue(sent.headers["Authorization"].startswith("Bearer "))

    def test_limit_and_cursor_are_forwarded_as_received(self) -> None:
        response = self.get(limit="diez", cursor="a+b/c=")

        self.assertEqual(response.status_code, 200)
        sent = self.requests[-1]
        self.assertEqual(sent.url.params["limit"], "diez")
        self.assertEqual(sent.url.params["cursor"], "a+b/c=")

    def test_no_parameter_selects_another_account(self) -> None:
        """CA3: el BFF no reenvía un `user_id` de la consulta; solo el token propio."""
        self.get(user_id=str(uuid.uuid4()))

        self.assertNotIn("user_id", self.requests[-1].url.params)

    def test_missing_token_is_401_without_calling_affiliation(self) -> None:
        for headers in ({}, {"Authorization": "Bearer otro"}):
            with self.subTest(headers=headers):
                response = self.client.get(PATH, headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["code"], "invalid_access_token")
        self.assertEqual(self.requests, [])

    def test_unknown_or_missing_actor_is_403_at_the_edge(self) -> None:
        for actor in (None, "administrator"):
            with self.subTest(actor=actor):
                response = self.get(actor)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["code"], "permission_required")
        self.assertEqual(self.requests, [])

    def test_service_errors_pass_unchanged(self) -> None:
        cases = (
            (403, error("permission_required")),
            (422, error("validation_error", [{"field": "query.limit", "issue": "out_of_range"}])),
            (
                422,
                error("validation_error", [{"field": "query.cursor", "issue": "invalid_cursor"}]),
            ),
        )
        for status, body in cases:
            with self.subTest(status=status, body=body):
                self.response = httpx.Response(status, json=body)
                response = self.get()
                self.assertEqual((response.status_code, response.json()), (status, body))
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_unexpected_status_and_transport_failures(self) -> None:
        self.response = httpx.Response(500, json=error("internal"))
        response = self.get()
        self.assertEqual(
            (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
        )

        for failure, status, code in (
            (httpx.ConnectError, 503, "upstream_unavailable"),
            (httpx.ReadTimeout, 504, "upstream_timeout"),
        ):
            with self.subTest(failure=failure.__name__):
                self.failure = failure
                response = self.get()
                self.assertEqual((response.status_code, response.json()["code"]), (status, code))


if __name__ == "__main__":
    unittest.main()
