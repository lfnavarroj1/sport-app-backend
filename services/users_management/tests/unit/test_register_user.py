"""Caso de uso HU001 con dobles en memoria (CA1-CA5)."""

import unittest
from datetime import timedelta
from uuid import UUID, uuid4

from fakes import (
    CURRENT_POLICIES,
    FakeHasher,
    FixedClock,
    InMemoryStore,
    InMemoryUnitOfWork,
    register_user_with,
)

from users_management.application.commands.register_user import (
    RegisterUser,
    RegisterUserCommand,
)
from users_management.application.errors import IdempotencyKeyConflict, IdempotencyKeyReused
from users_management.application.ports import IdempotencyRecord
from users_management.domain.account.errors import (
    EmailAlreadyRegistered,
    InvalidRegistrationData,
    PoliciesVersionMismatch,
)
from users_management.domain.account.model import ActorType


def command(**overrides: object) -> RegisterUserCommand:
    values: dict[str, object] = {
        "full_name": "Ana Pérez",
        "email": "Ana@Example.com",
        "password": "contraseña-larga-1",
        "actor_type": ActorType.ATHLETE,
        "accepted_policies_version": CURRENT_POLICIES,
        "idempotency_key": uuid4(),
        "correlation_id": UUID("11111111-1111-4111-8111-111111111111"),
    }
    values.update(overrides)
    return RegisterUserCommand(**values)  # type: ignore[arg-type]


class RegisterUserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryStore()
        self.hasher = FakeHasher()
        self.clock = FixedClock()
        self.register = register_user_with(self.store, self.hasher, self.clock)

    def test_creates_one_account_with_hash_outbox_and_idempotency(self) -> None:
        cmd = command()

        result = self.register(cmd)

        account = self.store.accounts[result.user_id]
        self.assertEqual(len(self.store.accounts), 1)
        self.assertEqual(account.email, "ana@example.com")
        self.assertEqual(account.password_hash, "$fake$hash-1")
        self.assertEqual(result.aggregate_version, 1)
        self.assertFalse(result.replayed)
        self.assertIn(cmd.idempotency_key, self.store.idempotency)

    def test_outbox_event_is_traceable_and_has_no_credentials(self) -> None:
        cmd = command()

        result = self.register(cmd)

        [event] = self.store.outbox
        self.assertEqual(event.event_type, "usuario.registrado.v1")
        self.assertEqual(event.aggregate_id, result.user_id)
        self.assertEqual(event.aggregate_version, 1)
        self.assertEqual(event.correlation_id, cmd.correlation_id)
        self.assertEqual(event.causation_id, str(cmd.idempotency_key))
        self.assertNotIn("password", event.payload)
        self.assertNotIn("contraseña-larga-1", str(event.payload))
        self.assertNotIn("$fake$", str(event.payload))

    def test_duplicate_email_is_rejected_without_new_records(self) -> None:
        self.register(command())

        with self.assertRaises(EmailAlreadyRegistered):
            self.register(command(email="  ANA@example.COM "))

        self.assertEqual(len(self.store.accounts), 1)
        self.assertEqual(len(self.store.outbox), 1)

    def test_invalid_data_persists_nothing_and_skips_hashing(self) -> None:
        cases = {
            "password": command(password="corta"),
            "full_name": command(full_name="   "),
            "email": command(email="sin-arroba"),
        }
        for field_name, invalid in cases.items():
            with self.subTest(field=field_name):
                with self.assertRaises(InvalidRegistrationData) as raised:
                    self.register(invalid)
                self.assertEqual(raised.exception.field, field_name)

        self.assertEqual(self.hasher.calls, 0)
        self.assertEqual(self.store.accounts, {})

    def test_outdated_policies_are_rejected(self) -> None:
        with self.assertRaises(PoliciesVersionMismatch):
            self.register(command(accepted_policies_version="2020-01"))

        self.assertEqual(self.store.accounts, {})

    def test_retry_with_same_key_returns_previous_result(self) -> None:
        cmd = command()
        first = self.register(cmd)

        second = self.register(cmd)

        self.assertTrue(second.replayed)
        self.assertEqual(second.user_id, first.user_id)
        self.assertEqual(second.created_at, first.created_at)
        self.assertEqual(len(self.store.accounts), 1)
        self.assertEqual(len(self.store.outbox), 1)
        self.assertEqual(self.hasher.calls, 1)

    def test_same_key_with_different_request_is_rejected(self) -> None:
        key = uuid4()
        self.register(command(idempotency_key=key))

        with self.assertRaises(IdempotencyKeyReused):
            self.register(command(idempotency_key=key, email="otra@example.com"))

        self.assertEqual(len(self.store.accounts), 1)

    def test_expired_key_is_treated_as_new_request(self) -> None:
        key = uuid4()
        self.register(command(idempotency_key=key))
        self.clock.advance(timedelta(hours=24, seconds=1))

        result = self.register(command(idempotency_key=key, email="otra@example.com"))

        self.assertFalse(result.replayed)
        self.assertEqual(len(self.store.accounts), 2)
        self.assertEqual(self.store.idempotency[key].resource_id, result.user_id)

    def _racing(self, hidden_reads: int) -> RegisterUser:
        """Las primeras lecturas de idempotencia no ven aún al ganador concurrente."""
        budget = {"hidden": hidden_reads}
        return RegisterUser(
            unit_of_work=lambda: _LateVisibilityUnitOfWork(self.store, budget),
            password_hasher=FakeHasher(),
            clock=self.clock,
            current_policies_version=CURRENT_POLICIES,
        )

    def test_concurrent_identical_request_returns_winner_result(self) -> None:
        cmd = command()
        winner = self.register(cmd)

        # La perdedora ve el correo ya registrado antes de ver la clave del ganador.
        result = self._racing(hidden_reads=2)(cmd)

        self.assertTrue(result.replayed)
        self.assertEqual(result.user_id, winner.user_id)
        self.assertEqual(len(self.store.accounts), 1)

    def test_concurrent_key_collision_on_commit_returns_winner_result(self) -> None:
        winner_cmd = command()
        winner = self.register(winner_cmd)
        # Mismo cuerpo, pero la perdedora llega hasta el COMMIT, que choca por la clave.
        self.store.accounts.clear()

        result = self._racing(hidden_reads=2)(winner_cmd)

        self.assertTrue(result.replayed)
        self.assertEqual(result.user_id, winner.user_id)

    def test_duplicate_email_from_another_key_is_still_rejected(self) -> None:
        self.register(command())

        with self.assertRaises(EmailAlreadyRegistered):
            self._racing(hidden_reads=0)(command())

    def test_unresolved_conflict_is_raised(self) -> None:
        failing = RegisterUser(
            unit_of_work=lambda: InMemoryUnitOfWork(
                self.store, fail_on_commit=IdempotencyKeyConflict()
            ),
            password_hasher=FakeHasher(),
            clock=self.clock,
            current_policies_version=CURRENT_POLICIES,
        )

        with self.assertRaises(IdempotencyKeyConflict):
            failing(command())

    def test_command_repr_never_contains_password(self) -> None:
        self.assertNotIn("contraseña-larga-1", repr(command()))

    def test_audits_success_and_rejection_without_personal_data(self) -> None:
        with self.assertLogs("sportapp.audit", level="INFO") as logs:
            self.register(command())
            with self.assertRaises(EmailAlreadyRegistered):
                self.register(command())

        records = [getattr(record, "fields", {}) for record in logs.records]
        self.assertEqual(records[0]["result"], "created")
        self.assertEqual(records[1], {"result": "rejected", "reason": "email_already_registered"})
        self.assertNotIn("ana@example.com", str(records))


class _LateVisibilityUnitOfWork(InMemoryUnitOfWork):
    def __init__(self, store: InMemoryStore, budget: dict[str, int]) -> None:
        super().__init__(store)
        self._budget = budget

    def find_idempotency(self, key: UUID) -> IdempotencyRecord | None:
        if self._budget["hidden"] > 0:
            self._budget["hidden"] -= 1
            return None
        return super().find_idempotency(key)


if __name__ == "__main__":
    unittest.main()
