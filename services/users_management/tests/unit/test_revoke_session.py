"""Caso de uso HU004 `RevokeSession` y su contrato HTTP, con persistencia en memoria."""

import unittest
import uuid
from datetime import timedelta

from fakes import (
    FakeTokenIssuer,
    FixedClock,
    InMemoryAuthStore,
    account,
    login_with,
    refresh_with,
    revoke_with,
)
from fastapi.testclient import TestClient

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from users_management.api.sessions import build_sessions_router
from users_management.application.commands.login import LoginCommand
from users_management.application.commands.refresh_session import RefreshSessionCommand
from users_management.application.commands.revoke_session import (
    RevocationResult,
    RevokeSessionCommand,
)
from users_management.application.commands.sessions import hash_refresh_token
from users_management.domain.authentication.errors import InvalidRefreshToken

PASSWORD = "contraseña-muy-secreta"


class RevokeSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryAuthStore()
        self.user = self.store.add_account(account())
        self.clock = FixedClock()
        self.issuer = FakeTokenIssuer()
        self.login = login_with(self.store, issuer=self.issuer, clock=self.clock)
        self.refresh = refresh_with(self.store, issuer=self.issuer, clock=self.clock)
        self.revoke = revoke_with(self.store, clock=self.clock)
        self.session = self.start()

    def start(self) -> object:
        return self.login(
            LoginCommand(email="ana@example.com", password=PASSWORD, client_ip="203.0.113.7")
        )

    def revoke_token(self, token: str) -> RevocationResult:
        return self.revoke(RevokeSessionCommand(refresh_token=token))

    def test_revokes_the_whole_session_and_blocks_renewal(self) -> None:
        """CA1 y CA3: la credencial renovable y sus sucesoras dejan de servir."""
        rotated = self.refresh(RefreshSessionCommand(refresh_token=self.session.refresh_token))  # type: ignore[attr-defined]

        result = self.revoke_token(rotated.refresh_token)

        self.assertEqual(result, RevocationResult.REVOKED)
        for token in (self.session.refresh_token, rotated.refresh_token):  # type: ignore[attr-defined]
            stored = self.store.token_by_hash(hash_refresh_token(token))
            assert stored is not None
            self.assertEqual(stored.revoked_at, self.clock.now())
        with self.assertRaises(InvalidRefreshToken):
            self.refresh(RefreshSessionCommand(refresh_token=rotated.refresh_token))

    def test_repeating_is_idempotent_and_does_not_reactivate(self) -> None:
        """CA2 y CA7."""
        token = self.session.refresh_token  # type: ignore[attr-defined]
        first = self.revoke_token(token)
        revoked_at = self.store.token_by_hash(hash_refresh_token(token)).revoked_at  # type: ignore[union-attr]
        self.clock.advance(timedelta(minutes=5))

        second = self.revoke_token(token)

        self.assertEqual(
            (first, second), (RevocationResult.REVOKED, RevocationResult.ALREADY_REVOKED)
        )
        stored = self.store.token_by_hash(hash_refresh_token(token))
        assert stored is not None
        self.assertEqual(stored.revoked_at, revoked_at, "no cambia la fecha de revocación")
        with self.assertRaises(InvalidRefreshToken):
            self.refresh(RefreshSessionCommand(refresh_token=token))

    def test_unknown_or_expired_tokens_are_safe(self) -> None:
        self.assertEqual(self.revoke_token("x" * 43), RevocationResult.UNKNOWN)

        self.clock.advance(timedelta(days=8))
        self.assertEqual(
            self.revoke_token(self.session.refresh_token),  # type: ignore[attr-defined]
            RevocationResult.REVOKED,
        )

    def test_only_the_closed_session_is_revoked(self) -> None:
        other = self.start()

        self.revoke_token(self.session.refresh_token)  # type: ignore[attr-defined]
        renewed = self.refresh(RefreshSessionCommand(refresh_token=other.refresh_token))  # type: ignore[attr-defined]

        self.assertTrue(renewed.refresh_token)

    def test_audit_records_identity_and_result_without_token(self) -> None:
        """CA4: fecha (registro), identidad y resultado; nunca el token."""
        token = self.session.refresh_token  # type: ignore[attr-defined]

        with self.assertLogs("sportapp.audit", level="INFO") as logs:
            self.revoke_token(token)
            self.revoke_token("y" * 43)

        revoked, unknown = (record.fields for record in logs.records)  # type: ignore[attr-defined]
        self.assertEqual(revoked["result"], "revoked")
        self.assertEqual(revoked["user_id"], str(self.user.user_id))
        self.assertIn("family_id", revoked)
        self.assertEqual(unknown, {"result": "unknown"})
        self.assertNotIn(token, str([r.__dict__ for r in logs.records]))
        self.assertNotIn(hash_refresh_token(token), str([r.__dict__ for r in logs.records]))
        self.assertIsNotNone(logs.records[0].created)

    def test_command_repr_hides_the_token(self) -> None:
        self.assertNotIn("secreto", repr(RevokeSessionCommand(refresh_token="secreto" * 5)))


class RevokeSessionApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryAuthStore()
        self.store.add_account(account())
        clock = FixedClock()
        issuer = FakeTokenIssuer()
        app = create_service_app(
            service_name="users-management",
            title="t",
            api_version="0",
            settings=ServiceSettings(app_env="test"),  # type: ignore[call-arg]
            routers=[
                build_sessions_router(
                    login_with(self.store, issuer=issuer, clock=clock),
                    refresh_with(self.store, issuer, clock),
                    revoke_with(self.store, clock),
                )
            ],
        )
        self.client = TestClient(app)

    def test_revoke_returns_204_and_refresh_is_rejected_afterwards(self) -> None:
        token = self.client.post(
            "/v1/sessions", json={"email": "ana@example.com", "password": PASSWORD}
        ).json()["refresh_token"]

        first = self.client.post("/v1/sessions/revoke", json={"refresh_token": token})
        again = self.client.post("/v1/sessions/revoke", json={"refresh_token": token})
        unknown = self.client.post("/v1/sessions/revoke", json={"refresh_token": "z" * 43})
        renewed = self.client.post("/v1/sessions/refresh", json={"refresh_token": token})

        for response in (first, again, unknown):
            self.assertEqual(response.status_code, 204)
            self.assertEqual(response.content, b"")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(renewed.status_code, 401)
        self.assertEqual(renewed.json()["code"], "invalid_refresh_token")

    def test_optional_idempotency_key_is_accepted_and_audited(self) -> None:
        """ADR-026: la cola offline móvil repite la misma clave; el resultado sigue siendo 204."""
        token = self.client.post(
            "/v1/sessions", json={"email": "ana@example.com", "password": PASSWORD}
        ).json()["refresh_token"]
        retry_id = str(uuid.uuid4())

        with self.assertLogs("sportapp.audit", level="INFO") as logs:
            first = self.client.post(
                "/v1/sessions/revoke",
                json={"refresh_token": token},
                headers={"Idempotency-Key": retry_id},
            )
            retried = self.client.post(
                "/v1/sessions/revoke",
                json={"refresh_token": token},
                headers={"Idempotency-Key": retry_id},
            )
        invalid = self.client.post(
            "/v1/sessions/revoke",
            json={"refresh_token": token},
            headers={"Idempotency-Key": "no-es-uuid"},
        )

        self.assertEqual((first.status_code, retried.status_code), (204, 204))
        results = [record.fields for record in logs.records]  # type: ignore[attr-defined]
        self.assertEqual([f["result"] for f in results], ["revoked", "already_revoked"])
        self.assertEqual({f["idempotency_key"] for f in results}, {retry_id})
        self.assertEqual(invalid.status_code, 422)

    def test_malformed_body_is_422_without_echo(self) -> None:
        for body in ({}, {"refresh_token": "corto"}, {"refresh_token": "x" * 43, "extra": 1}):
            with self.subTest(body=list(body)):
                response = self.client.post("/v1/sessions/revoke", json=body)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("corto", response.text)


if __name__ == "__main__":
    unittest.main()
