"""Caso de uso HU003 `Login`: CA1 a CA4 sin infraestructura."""

import unittest
from datetime import timedelta

from fakes import (
    FakeTokenIssuer,
    FakeVerifier,
    FixedClock,
    InMemoryAuthStore,
    account,
    login_with,
)

from users_management.application.commands.login import LoginCommand, throttle_key
from users_management.application.commands.sessions import hash_refresh_token
from users_management.domain.account.model import AccountStatus
from users_management.domain.authentication.errors import (
    InvalidCredentials,
    TooManyLoginAttempts,
)

PASSWORD = "contraseña-muy-secreta"
IP = "203.0.113.7"


def command(email: str = "ana@example.com", password: str = PASSWORD, ip: str = IP) -> LoginCommand:
    return LoginCommand(email=email, password=password, client_ip=ip)


class LoginTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryAuthStore()
        self.user = self.store.add_account(account())
        self.verifier = FakeVerifier()
        self.issuer = FakeTokenIssuer()
        self.clock = FixedClock()
        self.login = login_with(self.store, self.verifier, self.issuer, self.clock)

    def test_valid_credentials_return_identity_and_tokens_with_expiry(self) -> None:
        """CA1."""
        result = self.login(command(email="  ANA@Example.com "))

        self.assertEqual(result.user_id, self.user.user_id)
        self.assertEqual(result.actor_type, self.user.actor_type)
        self.assertEqual(result.access_token, "access-1")
        self.assertEqual(result.access_token_expires_in, 900)
        self.assertEqual(result.refresh_token_expires_in, 7 * 24 * 3600)
        self.assertEqual(self.issuer.issued, [(self.user.user_id, "athlete")])

    def test_only_the_refresh_token_hash_is_stored(self) -> None:
        """CA1: el valor entregado nunca se persiste."""
        result = self.login(command())

        [stored] = self.store.refresh_tokens.values()
        self.assertEqual(stored.token_hash, hash_refresh_token(result.refresh_token))
        self.assertNotEqual(stored.token_hash, result.refresh_token)
        self.assertEqual(stored.user_id, self.user.user_id)
        self.assertEqual(stored.expires_at, self.clock.now() + timedelta(days=7))

    def test_wrong_password_is_rejected_without_tokens(self) -> None:
        """CA2."""
        with self.assertRaises(InvalidCredentials):
            self.login(command(password="otra-contraseña-incorrecta"))

        self.assertEqual(self.issuer.issued, [])
        self.assertEqual(self.store.refresh_tokens, {})

    def test_unknown_email_gets_the_same_error_after_a_dummy_verification(self) -> None:
        """CA2 sin enumeración: también se verifica (contra un hash ficticio)."""
        with self.assertRaises(InvalidCredentials) as unknown:
            self.login(command(email="nadie@example.com"))
        with self.assertRaises(InvalidCredentials) as wrong:
            self.login(command(password="otra-contraseña-incorrecta"))

        self.assertEqual(self.verifier.calls, [None, self.user.password_hash])
        self.assertEqual(str(unknown.exception), str(wrong.exception))
        self.assertEqual(self.issuer.issued, [])

    def test_disabled_account_is_rejected_and_audited(self) -> None:
        """CA3: mismo error genérico, sin tokens, con registro seguro del intento."""
        disabled = self.store.add_account(
            account(email="baja@example.com", status=AccountStatus.DISABLED)
        )

        with (
            self.assertLogs("sportapp.audit", level="INFO") as audit,
            self.assertRaises(InvalidCredentials),
        ):
            self.login(command(email="baja@example.com"))

        self.assertEqual(self.issuer.issued, [])
        self.assertEqual(self.store.refresh_tokens, {})
        [record] = audit.records
        self.assertEqual(
            record.fields,  # type: ignore[attr-defined]
            {"result": "account_disabled", "user_id": str(disabled.user_id)},
        )
        self.assertNotIn("baja@example.com", record.getMessage())
        self.assertNotIn(PASSWORD, str(record.__dict__))

    def test_failures_above_the_policy_are_throttled_even_with_the_right_password(self) -> None:
        """CA4."""
        for _ in range(5):
            with self.assertRaises(InvalidCredentials):
                self.login(command(password="incorrecta"))

        with self.assertRaises(TooManyLoginAttempts) as limited:
            self.login(command())

        self.assertEqual(limited.exception.retry_after_seconds, 15 * 60)
        self.assertEqual(self.issuer.issued, [])
        self.assertEqual(len(self.verifier.calls), 5, "no se verifica durante el bloqueo")

    def test_throttle_ends_when_the_window_expires(self) -> None:
        """CA4: el control es temporal."""
        for _ in range(5):
            with self.assertRaises(InvalidCredentials):
                self.login(command(password="incorrecta"))

        self.clock.advance(timedelta(minutes=15))

        self.assertEqual(self.login(command()).user_id, self.user.user_id)

    def test_throttle_is_per_email_and_ip_so_accounts_cannot_be_locked_remotely(self) -> None:
        """CA4: un atacante desde otra IP no bloquea al titular."""
        for _ in range(6):
            with self.assertRaises((InvalidCredentials, TooManyLoginAttempts)):
                self.login(command(password="incorrecta", ip="198.51.100.66"))

        self.assertEqual(self.login(command(ip=IP)).user_id, self.user.user_id)

    def test_successful_login_resets_the_failure_counter(self) -> None:
        for _ in range(4):
            with self.assertRaises(InvalidCredentials):
                self.login(command(password="incorrecta"))

        self.login(command())

        self.assertNotIn(throttle_key("ana@example.com", IP), self.store.failures)

    def test_failure_window_key_does_not_contain_the_email(self) -> None:
        with self.assertRaises(InvalidCredentials):
            self.login(command(email="nadie@example.com"))

        [key] = self.store.failures
        self.assertNotIn("nadie", key)
        self.assertEqual(len(key), 64)

    def test_audit_records_never_contain_email_or_password(self) -> None:
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            with self.assertRaises(InvalidCredentials):
                self.login(command(password="incorrecta"))
            self.login(command())

        results = [record.fields["result"] for record in audit.records]  # type: ignore[attr-defined]
        self.assertEqual(results, ["invalid_credentials", "success"])
        for record in audit.records:
            self.assertNotIn("ana@example.com", str(record.__dict__))
            self.assertNotIn(PASSWORD, str(record.__dict__))

    def test_command_repr_hides_credentials(self) -> None:
        self.assertNotIn(PASSWORD, repr(command()))
        self.assertNotIn("ana@example.com", repr(command()))


if __name__ == "__main__":
    unittest.main()
