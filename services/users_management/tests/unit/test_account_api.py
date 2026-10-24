"""Contrato HTTP de la cuenta (HU002, ADR-035) con los casos de uso reales y persistencia en
memoria; el token se firma y se verifica de verdad."""

import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from account_fakes import (
    InMemoryAccountReader,
    InMemoryAccountStore,
    account_details,
    update_account_with,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fakes import FixedClock
from fastapi.testclient import TestClient

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from sportapp_platform.tokens import AccessTokenVerifier
from users_management.api.account import build_account_router
from users_management.application.errors import ReadModelUnavailable
from users_management.application.ports import AccountView
from users_management.application.queries.get_account import GetAccount
from users_management.domain.account.model import ActorType
from users_management.infrastructure.security.access_tokens import Ed25519AccessTokenIssuer

ISSUER = "sportapp-users-management"
AUDIENCE = "sportapp"
TOKENS = Ed25519AccessTokenIssuer(
    Ed25519PrivateKey.generate(), issuer=ISSUER, audience=AUDIENCE, ttl=timedelta(minutes=15)
)
VERIFIER = AccessTokenVerifier(TOKENS.jwks(), issuer=ISSUER, audience=AUDIENCE)
ACCOUNT = "/v1/me/account"


def bearer(user_id: UUID, actor_type: str = "athlete") -> dict[str, str]:
    token = TOKENS.issue(user_id, actor_type, datetime.now(UTC)).token
    return {"Authorization": f"Bearer {token}"}


def body(**overrides: Any) -> dict[str, Any]:
    return {
        "expected_version": 1,
        "full_name": "Ana María Pérez",
        "email": "ana.maria@example.com",
        **overrides,
    }


class AccountApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryAccountStore()
        self.reader = InMemoryAccountReader()
        app = create_service_app(
            service_name="users-management",
            title="t",
            api_version="0",
            settings=ServiceSettings(app_env="test"),  # type: ignore[call-arg]
            routers=[
                build_account_router(
                    verify_token=VERIFIER.verify,
                    update_account=update_account_with(self.store, FixedClock()),
                    get_account=GetAccount(self.reader),
                )
            ],
        )
        self.client = TestClient(app)
        self.account = self.store.add(account_details(actor_type=ActorType.ORGANIZER))
        self.auth = bearer(self.account.user_id, "organizer")

    def put(
        self,
        payload: Any,
        key: str | None = "new",
        headers: dict[str, str] | None = None,
    ) -> Any:
        request_headers = dict(self.auth if headers is None else headers)
        if key is not None:
            request_headers["Idempotency-Key"] = str(uuid4()) if key == "new" else key
        if isinstance(payload, (bytes, str)):
            return self.client.put(ACCOUNT, content=payload, headers=request_headers)
        return self.client.put(ACCOUNT, json=payload, headers=request_headers)

    def details(self, response: Any) -> set[tuple[str | None, str]]:
        return {(item["field"], item["issue"]) for item in response.json()["details"]}

    def test_every_actor_edits_its_own_account(self) -> None:
        """CA1: `200` con la cuenta confirmada y su versión nueva, para los tres actores."""
        for actor in ActorType:
            with self.subTest(actor=actor):
                account = self.store.add(
                    account_details(actor_type=actor, email=f"{actor.value}@example.com")
                )
                response = self.put(
                    body(email=f"Nuevo.{actor.value}@Example.com"),
                    headers=bearer(account.user_id, actor.value),
                )

                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                document = response.json()
                self.assertEqual(document["user_id"], str(account.user_id))
                self.assertEqual(document["email"], f"nuevo.{actor.value}@example.com")
                self.assertEqual((document["version"], document["actor_type"]), (2, actor.value))
                self.assertTrue(document["updated_at"].endswith("Z"))

    def test_forbidden_fields_are_rejected_without_changes(self) -> None:
        """CA2: no se puede enviar `user_id`, `actor_type`, `status` ni contraseña."""
        response = self.put(
            body(
                user_id=str(uuid4()),
                actor_type="athlete",
                status="disabled",
                password="contraseña-muy-secreta",
            )
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "validation_error")
        self.assertEqual(
            self.details(response),
            {
                ("user_id", "field_not_allowed"),
                ("actor_type", "field_not_allowed"),
                ("status", "field_not_allowed"),
                ("password", "field_not_allowed"),
            },
        )
        self.assertEqual(self.store.accounts[self.account.user_id], self.account)
        self.assertEqual(self.store.outbox, [])

    def test_identity_comes_from_the_token(self) -> None:
        """CA2: otra cuenta no se puede tocar; la identidad sale siempre del token."""
        other = self.store.add(account_details(email="otra@example.com"))

        self.put(body())

        self.assertEqual(self.store.accounts[other.user_id], other)
        self.assertEqual(self.store.accounts[self.account.user_id].version, 2)

    def test_missing_or_invalid_token(self) -> None:
        for headers in ({}, {"Authorization": "Bearer no-es-un-token"}, {"Authorization": "x"}):
            with self.subTest(headers=headers):
                response = self.put(body(), headers=headers)

                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["code"], "invalid_access_token")
                self.assertEqual(response.headers["cache-control"], "no-store")

    def test_token_of_an_unknown_account_is_unauthenticated(self) -> None:
        response = self.put(body(), headers=bearer(uuid4()))

        self.assertEqual(response.status_code, 401)

    def test_validation_errors(self) -> None:
        cases: list[tuple[Any, set[tuple[str | None, str]]]] = [
            (
                {},
                {
                    ("expected_version", "required"),
                    ("full_name", "required"),
                    ("email", "required"),
                },
            ),
            (body(expected_version="1"), {("expected_version", "unknown_value")}),
            (body(expected_version=True), {("expected_version", "unknown_value")}),
            (body(expected_version=0), {("expected_version", "out_of_range")}),
            (body(full_name=5), {("full_name", "unknown_value")}),
            (body(full_name=" "), {("full_name", "invalid_length")}),
            (body(full_name="x" * 121), {("full_name", "invalid_length")}),
            (body(email="sin-arroba"), {("email", "unknown_value")}),
            (body(email=["a@example.com"]), {("email", "unknown_value")}),
            (body(email="a" * 250 + "@x.co"), {("email", "invalid_length")}),
            ([], {(None, "unknown_value")}),
            (b"", {(None, "required")}),
            (b"{no-json", {(None, "unknown_value")}),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                response = self.put(payload)

                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self.details(response), expected)
        self.assertEqual(self.store.outbox, [])

    def test_idempotency_key_is_required_and_reported_with_the_body(self) -> None:
        missing = self.put(body(full_name=""), key=None)
        invalid = self.put(body(), key="no-uuid")

        self.assertEqual(
            self.details(missing),
            {("Idempotency-Key", "required"), ("full_name", "invalid_length")},
        )
        self.assertEqual(self.details(invalid), {("Idempotency-Key", "unknown_value")})

    def test_email_of_another_account_is_a_generic_conflict(self) -> None:
        """CA3: mismo código y mensaje que el registro; nada de la otra cuenta."""
        other = self.store.add(account_details(email="ocupado@example.com", full_name="Otra"))

        response = self.put(body(email="ocupado@example.com"))

        self.assertEqual(response.status_code, 409)
        document = response.json()
        self.assertEqual(document["code"], "email_already_registered")
        self.assertEqual(document["message"], "El correo ya está registrado.")
        self.assertEqual(document["details"], [])
        self.assertNotIn(str(other.user_id), response.text)
        self.assertEqual(self.store.accounts[self.account.user_id].email, "ana@example.com")

    def test_stale_version_reports_the_current_one(self) -> None:
        """CA4."""
        response = self.put(body(expected_version=3))

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "version_conflict")
        self.assertEqual(self.details(response), {("expected_version", "1")})

    def test_replay_returns_the_original_response(self) -> None:
        """CA5."""
        key = str(uuid4())
        first = self.put(body(), key=key)
        again = self.put(body(), key=key)
        reused = self.put(body(full_name="Otro"), key=key)

        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json(), first.json())
        self.assertEqual(again.headers["idempotent-replayed"], "true")
        self.assertNotIn("idempotent-replayed", first.headers)
        self.assertEqual(len(self.store.outbox), 1)
        self.assertEqual(
            (reused.status_code, reused.json()["code"]), (409, "idempotency_key_reused")
        )

    def test_same_email_answers_the_current_account(self) -> None:
        response = self.put(body(full_name=self.account.full_name, email=" ANA@example.com"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["version"], 1)
        self.assertEqual(self.store.outbox, [])


class AccountReadApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = InMemoryAccountReader()
        app = create_service_app(
            service_name="users-management",
            title="t",
            api_version="0",
            settings=ServiceSettings(app_env="test"),  # type: ignore[call-arg]
            routers=[
                build_account_router(
                    verify_token=VERIFIER.verify,
                    update_account=update_account_with(InMemoryAccountStore()),
                    get_account=GetAccount(self.reader),
                )
            ],
        )
        self.client = TestClient(app)
        self.user_id = uuid4()

    def test_reads_the_projected_account(self) -> None:
        self.reader.views[self.user_id] = AccountView(
            user_id=self.user_id,
            full_name="Ana Pérez",
            email="ana@example.com",
            actor_type="service_provider",
            status="active",
            version=3,
            projected_at=datetime(2026, 10, 24, 10, 0, tzinfo=UTC),
        )

        response = self.client.get(ACCOUNT, headers=bearer(self.user_id, "service_provider"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(
            response.json(),
            {
                "user_id": str(self.user_id),
                "full_name": "Ana Pérez",
                "email": "ana@example.com",
                "actor_type": "service_provider",
                "status": "active",
                "version": 3,
                "projected_at": "2026-10-24T10:00:00Z",
            },
        )

    def test_not_projected_yet(self) -> None:
        response = self.client.get(ACCOUNT, headers=bearer(self.user_id))

        self.assertEqual((response.status_code, response.json()["code"]), (404, "user_not_found"))
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_read_model_down(self) -> None:
        self.reader.unavailable = ReadModelUnavailable()

        response = self.client.get(ACCOUNT, headers=bearer(self.user_id))

        self.assertEqual(
            (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
        )

    def test_requires_a_token(self) -> None:
        response = self.client.get(ACCOUNT)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["cache-control"], "no-store")
