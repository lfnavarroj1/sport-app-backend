import unittest
from datetime import UTC, datetime
from uuid import uuid4

from users_management.domain.account.errors import (
    InvalidRegistrationData,
    PoliciesVersionMismatch,
)
from users_management.domain.account.events import UserRegistered
from users_management.domain.account.model import (
    AccountStatus,
    ActorType,
    UserAccount,
    normalize_email,
    normalize_full_name,
    validate_password,
    validate_policies_acceptance,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


class EmailRulesTest(unittest.TestCase):
    def test_normalizes_case_and_surrounding_spaces(self) -> None:
        self.assertEqual(normalize_email("  Ana.Perez@Example.COM "), "ana.perez@example.com")

    def test_rejects_empty_or_too_long_email(self) -> None:
        for value in ("", "   ", "sin-arroba", "a@" + "b" * 253):
            with self.subTest(value=value[:20]), self.assertRaises(InvalidRegistrationData):
                normalize_email(value)


class FullNameRulesTest(unittest.TestCase):
    def test_collapses_internal_whitespace(self) -> None:
        self.assertEqual(normalize_full_name("  Ana   María  Pérez "), "Ana María Pérez")

    def test_rejects_blank_and_too_long_names(self) -> None:
        for value in ("", "   ", "x" * 121):
            with self.subTest(length=len(value)), self.assertRaises(InvalidRegistrationData):
                normalize_full_name(value)

    def test_accepts_name_at_maximum_length(self) -> None:
        self.assertEqual(len(normalize_full_name("x" * 120)), 120)


class PasswordRulesTest(unittest.TestCase):
    def test_length_bounds_without_composition_rules(self) -> None:
        validate_password("a" * 12)
        validate_password("a" * 128)
        for length in (11, 129):
            with self.subTest(length=length), self.assertRaises(InvalidRegistrationData) as raised:
                validate_password("a" * length)
            self.assertEqual(raised.exception.field, "password")


class PoliciesTest(unittest.TestCase):
    def test_requires_current_policies_version(self) -> None:
        validate_policies_acceptance("2026-10", "2026-10")
        with self.assertRaises(PoliciesVersionMismatch):
            validate_policies_acceptance("2025-01", "2026-10")


class RegisterAccountTest(unittest.TestCase):
    def test_new_account_is_active_in_version_one(self) -> None:
        account = UserAccount.register(
            user_id=uuid4(),
            full_name=" Ana  Pérez ",
            email="ANA@example.com",
            actor_type=ActorType.ORGANIZER,
            password_hash="$argon2id$...",
            accepted_policies_version="2026-10",
            now=NOW,
        )

        self.assertEqual(account.status, AccountStatus.ACTIVE)
        self.assertEqual(account.version, 1)
        self.assertEqual(account.full_name, "Ana Pérez")
        self.assertEqual(account.email, "ana@example.com")
        self.assertEqual(account.policies_accepted_at, NOW)

    def test_registered_event_payload_excludes_credentials(self) -> None:
        account = UserAccount.register(
            user_id=uuid4(),
            full_name="Ana",
            email="ana@example.com",
            actor_type=ActorType.ATHLETE,
            password_hash="$argon2id$secreto",
            accepted_policies_version="2026-10",
            now=NOW,
        )

        payload = UserRegistered.from_account(account).payload()

        self.assertEqual(
            set(payload),
            {"user_id", "full_name", "email", "actor_type", "status", "registered_at"},
        )
        self.assertNotIn("argon2", str(payload))


if __name__ == "__main__":
    unittest.main()
