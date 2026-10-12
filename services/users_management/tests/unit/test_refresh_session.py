"""Caso de uso HU003 `RefreshSession`: renovación controlada con rotación (ADR-009)."""

import unittest
from dataclasses import replace
from datetime import timedelta

from fakes import (
    FakeTokenIssuer,
    FixedClock,
    InMemoryAuthStore,
    account,
    login_with,
    refresh_with,
)

from users_management.application.commands.login import LoginCommand
from users_management.application.commands.refresh_session import RefreshSessionCommand
from users_management.application.commands.sessions import hash_refresh_token
from users_management.domain.account.model import AccountStatus
from users_management.domain.authentication.errors import (
    InvalidRefreshToken,
    RefreshTokenReused,
)


class RefreshSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryAuthStore()
        self.user = self.store.add_account(account())
        self.clock = FixedClock()
        self.issuer = FakeTokenIssuer()
        login = login_with(self.store, issuer=self.issuer, clock=self.clock)
        self.session = login(
            LoginCommand(
                email="ana@example.com", password="contraseña-muy-secreta", client_ip="203.0.113.7"
            )
        )
        self.refresh = refresh_with(self.store, issuer=self.issuer, clock=self.clock)

    def renew(self, token: str) -> object:
        return self.refresh(RefreshSessionCommand(refresh_token=token))

    def test_rotates_the_token_and_issues_a_new_access_token(self) -> None:
        self.clock.advance(timedelta(minutes=20))

        renewed = self.refresh(RefreshSessionCommand(refresh_token=self.session.refresh_token))

        self.assertNotEqual(renewed.refresh_token, self.session.refresh_token)
        self.assertEqual(renewed.access_token, "access-2")
        self.assertEqual(renewed.user_id, self.user.user_id)
        previous = self.store.token_by_hash(hash_refresh_token(self.session.refresh_token))
        current = self.store.token_by_hash(hash_refresh_token(renewed.refresh_token))
        assert previous is not None
        assert current is not None
        self.assertEqual(previous.used_at, self.clock.now())
        self.assertEqual(current.family_id, previous.family_id)
        self.assertEqual(current.expires_at, self.clock.now() + timedelta(days=7))

    def test_reusing_a_rotated_token_revokes_the_whole_family(self) -> None:
        renewed = self.refresh(RefreshSessionCommand(refresh_token=self.session.refresh_token))

        with self.assertRaises(RefreshTokenReused):
            self.renew(self.session.refresh_token)
        with self.assertRaises(InvalidRefreshToken):
            self.renew(renewed.refresh_token)

        self.assertTrue(all(t.revoked_at for t in self.store.refresh_tokens.values()))

    def test_unknown_or_expired_tokens_are_rejected(self) -> None:
        with self.assertRaises(InvalidRefreshToken):
            self.renew("token-que-nunca-se-emitio-0000")

        self.clock.advance(timedelta(days=7))

        with self.assertRaises(InvalidRefreshToken):
            self.renew(self.session.refresh_token)
        self.assertEqual(self.issuer.issued, [(self.user.user_id, "athlete")])

    def test_disabled_account_cannot_renew_and_loses_its_sessions(self) -> None:
        self.store.accounts[self.user.user_id] = replace(self.user, status=AccountStatus.DISABLED)

        with self.assertRaises(InvalidRefreshToken):
            self.renew(self.session.refresh_token)

        [token] = self.store.refresh_tokens.values()
        self.assertIsNotNone(token.revoked_at)

    def test_reuse_is_audited_without_the_token(self) -> None:
        self.renew(self.session.refresh_token)

        with (
            self.assertLogs("sportapp.audit", level="INFO") as audit,
            self.assertRaises(RefreshTokenReused),
        ):
            self.renew(self.session.refresh_token)

        [record] = audit.records
        self.assertEqual(record.fields["result"], "reuse_detected")  # type: ignore[attr-defined]
        self.assertNotIn(self.session.refresh_token, str(record.__dict__))


if __name__ == "__main__":
    unittest.main()
