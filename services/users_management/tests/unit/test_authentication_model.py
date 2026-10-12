"""Reglas de dominio de HU003: control de abuso, estado de la cuenta y refresh token."""

import unittest
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fakes import account

from users_management.domain.account.model import AccountStatus
from users_management.domain.authentication.errors import (
    InvalidRefreshToken,
    RefreshTokenReused,
)
from users_management.domain.authentication.model import (
    FailureWindow,
    LoginThrottlePolicy,
    RefreshToken,
)

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
POLICY = LoginThrottlePolicy(max_failures=5, window=timedelta(minutes=15))


class LoginThrottlePolicyTest(unittest.TestCase):
    def test_allows_attempts_below_the_limit(self) -> None:
        self.assertIsNone(POLICY.retry_after(None, NOW))
        self.assertIsNone(POLICY.retry_after(FailureWindow(4, NOW), NOW))

    def test_blocks_until_the_window_expires(self) -> None:
        window = FailureWindow(5, NOW - timedelta(minutes=10))

        self.assertEqual(POLICY.retry_after(window, NOW), 300)
        self.assertEqual(POLICY.retry_after(window, NOW + timedelta(seconds=299.2)), 1)
        self.assertIsNone(POLICY.retry_after(window, NOW + timedelta(minutes=5)))

    def test_failures_accumulate_inside_the_window_and_restart_after_it(self) -> None:
        first = POLICY.after_failure(None, NOW)
        second = POLICY.after_failure(first, NOW + timedelta(minutes=14))
        restarted = POLICY.after_failure(second, NOW + timedelta(minutes=15))

        self.assertEqual(first, FailureWindow(1, NOW))
        self.assertEqual(second, FailureWindow(2, NOW))
        self.assertEqual(restarted, FailureWindow(1, NOW + timedelta(minutes=15)))


class AccountStatusTest(unittest.TestCase):
    def test_only_active_accounts_can_authenticate(self) -> None:
        self.assertTrue(account().can_authenticate)
        self.assertFalse(account(status=AccountStatus.DISABLED).can_authenticate)


class RefreshTokenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.token = RefreshToken.issue(
            token_id=uuid.uuid4(),
            family_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            token_hash="h" * 64,
            now=NOW,
            ttl=timedelta(days=7),
        )

    def test_new_token_is_usable_until_it_expires(self) -> None:
        self.assertEqual(self.token.expires_at, NOW + timedelta(days=7))
        self.token.ensure_usable(NOW + timedelta(days=6))
        with self.assertRaises(InvalidRefreshToken):
            self.token.ensure_usable(NOW + timedelta(days=7))

    def test_revoked_token_is_invalid(self) -> None:
        with self.assertRaises(InvalidRefreshToken) as error:
            replace(self.token, revoked_at=NOW).ensure_usable(NOW)
        self.assertNotIsInstance(error.exception, RefreshTokenReused)

    def test_used_token_signals_reuse(self) -> None:
        with self.assertRaises(RefreshTokenReused):
            replace(self.token, used_at=NOW).ensure_usable(NOW)


if __name__ == "__main__":
    unittest.main()
