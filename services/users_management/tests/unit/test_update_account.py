"""HU002 (ADR-035): reglas de la cuenta editable y caso de uso `UpdateAccount` sin
infraestructura."""

import logging
import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from account_fakes import (
    InMemoryAccountStore,
    account_details,
    update_account_with,
)
from fakes import FixedClock

from users_management.application.commands.update_account import (
    OPERATION,
    UpdateAccountCommand,
)
from users_management.application.errors import AccountNotFound, IdempotencyKeyReused
from users_management.domain.account.editing import AccountChangeInput, validate_account_change
from users_management.domain.account.errors import (
    AccountVersionConflict,
    EmailAlreadyRegistered,
)
from users_management.domain.account.events import AGGREGATE_TYPE, UserUpdated
from users_management.domain.profile.validation import InvalidProfileSection, Issue, Violation

NOW = datetime(2026, 10, 24, 15, 30, tzinfo=UTC)


def change(**overrides: object) -> AccountChangeInput:
    values: dict[str, object] = {
        "expected_version": 1,
        "full_name": "Ana María Pérez",
        "email": "ana.maria@example.com",
        **overrides,
    }
    return AccountChangeInput(**values)  # type: ignore[arg-type]


def issues(error: InvalidProfileSection) -> set[tuple[str | None, str]]:
    return {(item.field, item.issue.value) for item in error.violations}


class AccountChangeValidationTest(unittest.TestCase):
    """Normalización y longitudes (unitarias de la ficha)."""

    def test_normalizes_name_and_email(self) -> None:
        valid = validate_account_change(
            change(full_name="  Ana   María  Pérez ", email="  Ana.Maria@Example.COM ")
        )

        self.assertEqual(valid.full_name, "Ana María Pérez")
        self.assertEqual(valid.email, "ana.maria@example.com")
        self.assertEqual(valid.expected_version, 1)

    def test_lengths_are_limited(self) -> None:
        self.assertEqual(validate_account_change(change(full_name="x" * 120)).full_name, "x" * 120)
        with self.assertRaises(InvalidProfileSection) as caught:
            validate_account_change(change(full_name="x" * 121, email="a" * 250 + "@x.co"))

        self.assertEqual(
            issues(caught.exception),
            {("full_name", "invalid_length"), ("email", "invalid_length")},
        )

    def test_blank_name_is_invalid_length(self) -> None:
        with self.assertRaises(InvalidProfileSection) as caught:
            validate_account_change(change(full_name="   "))

        self.assertEqual(issues(caught.exception), {("full_name", "invalid_length")})

    def test_all_fields_are_required_and_reported_together(self) -> None:
        with self.assertRaises(InvalidProfileSection) as caught:
            validate_account_change(AccountChangeInput())

        self.assertEqual(
            issues(caught.exception),
            {("expected_version", "required"), ("full_name", "required"), ("email", "required")},
        )

    def test_expected_version_must_be_positive(self) -> None:
        with self.assertRaises(InvalidProfileSection) as caught:
            validate_account_change(change(expected_version=0))

        self.assertEqual(issues(caught.exception), {("expected_version", "out_of_range")})

    def test_transport_violations_are_not_reported_twice(self) -> None:
        with self.assertRaises(InvalidProfileSection) as caught:
            validate_account_change(
                change(email=None, transport_violations=(Violation("email", Issue.UNKNOWN_VALUE),))
            )

        self.assertEqual(issues(caught.exception), {("email", "unknown_value")})


class AccountDetailsTest(unittest.TestCase):
    def test_change_creates_the_next_version(self) -> None:
        current = account_details(version=3)

        updated, changed = current.change(
            expected_version=3, full_name="Ana Pérez", email="nuevo@example.com", now=NOW
        )

        self.assertEqual(changed, ("email",))
        self.assertEqual(
            (updated.version, updated.email, updated.updated_at), (4, "nuevo@example.com", NOW)
        )
        self.assertEqual((updated.actor_type, updated.status), (current.actor_type, current.status))

    def test_same_values_do_not_create_a_version(self) -> None:
        current = account_details(version=2)

        updated, changed = current.change(
            expected_version=2, full_name=current.full_name, email=current.email, now=NOW
        )

        self.assertEqual(changed, ())
        self.assertIs(updated, current)

    def test_stale_version_conflicts_even_without_changes(self) -> None:
        current = account_details(version=5)

        with self.assertRaises(AccountVersionConflict) as caught:
            current.change(
                expected_version=4, full_name=current.full_name, email=current.email, now=NOW
            )

        self.assertEqual(caught.exception.current_version, 5)

    def test_repr_has_no_personal_data(self) -> None:
        self.assertNotIn("ana@example.com", repr(account_details()))


class UpdateAccountTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryAccountStore()
        self.clock = FixedClock(NOW)
        self.account = self.store.add(account_details())
        self.use_case = update_account_with(self.store, self.clock)

    def run_command(self, key: object = None, **overrides: object) -> object:
        return self.use_case(
            self.account.user_id,
            UpdateAccountCommand(change=change(**overrides), idempotency_key=key or uuid4()),  # type: ignore[arg-type]
        )

    def test_updates_name_and_email_and_records_the_event(self) -> None:
        """CA1 y CA5: nueva versión, outbox con estado completo y sin hash ni políticas."""
        result = self.run_command()

        self.assertEqual(
            result.document,  # type: ignore[attr-defined]
            {
                "user_id": str(self.account.user_id),
                "full_name": "Ana María Pérez",
                "email": "ana.maria@example.com",
                "actor_type": "athlete",
                "status": "active",
                "version": 2,
                "updated_at": "2026-10-24T15:30:00Z",
            },
        )
        stored = self.store.accounts[self.account.user_id]
        self.assertEqual((stored.version, stored.email), (2, "ana.maria@example.com"))
        [message] = self.store.outbox
        self.assertEqual(message.event_type, UserUpdated.EVENT_TYPE)
        self.assertEqual(message.aggregate_type, AGGREGATE_TYPE)
        self.assertEqual(
            (message.aggregate_id, message.aggregate_version), (self.account.user_id, 2)
        )
        self.assertEqual(message.payload, result.document)  # type: ignore[attr-defined]
        self.assertNotIn("password_hash", message.payload)
        self.assertNotIn("accepted_policies_version", message.payload)

    def test_same_email_is_not_an_error_and_creates_no_version(self) -> None:
        result = self.run_command(full_name=self.account.full_name, email="ANA@example.com ")

        self.assertEqual(result.version, 1)  # type: ignore[attr-defined]
        self.assertEqual(self.store.outbox, [])
        self.assertEqual(self.store.accounts[self.account.user_id], self.account)

    def test_only_the_name_changes(self) -> None:
        result = self.run_command(email=self.account.email)

        self.assertEqual(result.changed_fields, ("full_name",))  # type: ignore[attr-defined]
        self.assertEqual(len(self.store.outbox), 1)

    def test_stale_version_is_rejected_without_changes(self) -> None:
        """CA4."""
        with self.assertRaises(AccountVersionConflict) as caught:
            self.run_command(expected_version=7)

        self.assertEqual(caught.exception.current_version, 1)
        self.assertEqual(self.store.accounts[self.account.user_id], self.account)
        self.assertEqual((self.store.outbox, self.store.idempotency), ([], {}))

    def test_email_of_another_account_is_rejected(self) -> None:
        """CA3: sin modificar la cuenta ni publicar."""
        self.store.add(account_details(email="ocupado@example.com"))

        with self.assertRaises(EmailAlreadyRegistered):
            self.run_command(email="Ocupado@Example.com")

        self.assertEqual(self.store.accounts[self.account.user_id], self.account)
        self.assertEqual(self.store.outbox, [])

    def test_email_taken_concurrently_is_rejected_at_commit(self) -> None:
        """CA3: otra cuenta confirma el mismo correo entre la comprobación y el `commit`."""
        rival = account_details(email="libre@example.com")
        self.store.add(account_details(email="otro@example.com", user_id=rival.user_id))
        use_case = update_account_with(
            self.store, self.clock, before_commit=lambda: self.store.add(rival)
        )

        with self.assertRaises(EmailAlreadyRegistered):
            use_case(
                self.account.user_id,
                UpdateAccountCommand(
                    change=change(email="libre@example.com"), idempotency_key=uuid4()
                ),
            )

        self.assertEqual(self.store.accounts[self.account.user_id], self.account)

    def test_same_key_and_payload_returns_the_original_response(self) -> None:
        """CA5: un reintento no crea otra versión ni otro evento."""
        key = uuid4()
        first = self.run_command(key)
        again = self.run_command(key)

        self.assertTrue(again.replayed)  # type: ignore[attr-defined]
        self.assertEqual(again.document, first.document)  # type: ignore[attr-defined]
        self.assertEqual(len(self.store.outbox), 1)
        record = self.store.idempotency[key]
        self.assertEqual((record.operation, record.resource_id), (OPERATION, self.account.user_id))
        self.assertEqual(record.expires_at - record.created_at, timedelta(hours=24))

    def test_same_key_with_another_payload_is_reused(self) -> None:
        key = uuid4()
        self.run_command(key)

        with self.assertRaises(IdempotencyKeyReused):
            self.run_command(key, full_name="Otro Nombre", expected_version=2)

    def test_same_key_by_another_account_is_reused(self) -> None:
        key = uuid4()
        self.run_command(key)
        other = self.store.add(account_details(email="otra@example.com"))

        with self.assertRaises(IdempotencyKeyReused):
            self.use_case(other.user_id, UpdateAccountCommand(change=change(), idempotency_key=key))

    def test_expired_key_is_executed_again(self) -> None:
        key = uuid4()
        self.run_command(key)
        self.clock.advance(timedelta(hours=24, seconds=1))

        with self.assertRaises(AccountVersionConflict):
            self.run_command(key)  # La cuenta ya va en la versión 2.

    def test_unknown_account_is_not_found(self) -> None:
        with self.assertRaises(AccountNotFound):
            self.use_case(uuid4(), UpdateAccountCommand(change=change(), idempotency_key=uuid4()))

    def test_audit_lists_changed_fields_without_values(self) -> None:
        """CA5: auditoría `audit.account_updated` sin valores."""
        with self.assertLogs("sportapp.audit", logging.INFO) as logs:
            self.run_command()
            with self.assertRaises(AccountVersionConflict):
                self.run_command(expected_version=9)

        updated, rejected = (record.__dict__["fields"] for record in logs.records)
        self.assertEqual(logs.records[0].getMessage(), "audit.account_updated")
        self.assertEqual(updated["changed_fields"], ["full_name", "email"])
        self.assertEqual((updated["account_version"], updated["result"]), (2, "updated"))
        self.assertEqual((rejected["result"], rejected["reason"]), ("rejected", "version_conflict"))
        text = " ".join(str(record.__dict__) for record in logs.records)
        self.assertNotIn("ana.maria@example.com", text)
        self.assertNotIn("Ana María", text)
