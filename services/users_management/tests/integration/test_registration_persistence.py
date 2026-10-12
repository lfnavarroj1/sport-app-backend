"""HU001 contra PostgreSQL real: CA1, CA2, CA4 y CA5, incluida concurrencia."""

import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from argon2 import PasswordHasher
from sqlalchemy import select
from sqlalchemy.exc import DataError
from support import WriteDatabase, registration, write_database

from users_management.application.commands.register_user import RegistrationResult
from users_management.application.ports import OutboxMessage
from users_management.domain.account.errors import EmailAlreadyRegistered
from users_management.infrastructure.persistence_write.models import (
    IdempotencyKeyRow,
    OutboxEventRow,
    UserAccountRow,
)
from users_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from users_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyRegistrationUnitOfWork,
)

CONCURRENCY = 6


class RegistrationPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: WriteDatabase = self.enterContext(write_database("users_hu001"))
        self.register = self.db.register_user()

    def test_account_outbox_and_idempotency_are_committed_together(self) -> None:
        cmd = registration(email="Ana.Perez@Example.com")

        result = self.register(cmd)

        with self.db.sessions() as session:
            account = session.get_one(UserAccountRow, result.user_id)
            [event] = session.scalars(select(OutboxEventRow)).all()
            idempotency = session.get_one(IdempotencyKeyRow, cmd.idempotency_key)
        self.assertEqual(account.email, "ana.perez@example.com")
        self.assertEqual(account.version, 1)
        self.assertEqual(event.aggregate_id, result.user_id)
        self.assertEqual(event.event_type, "usuario.registrado.v1")
        self.assertEqual(event.correlation_id, cmd.correlation_id)
        self.assertIsNone(event.published_at)
        self.assertEqual(idempotency.resource_id, result.user_id)

    def test_password_is_stored_only_as_argon2id_hash(self) -> None:
        cmd = registration()

        result = self.register(cmd)

        with self.db.sessions() as session:
            account = session.get_one(UserAccountRow, result.user_id)
            event = session.scalars(select(OutboxEventRow)).one()
            idempotency = session.get_one(IdempotencyKeyRow, cmd.idempotency_key)
        self.assertTrue(account.password_hash.startswith("$argon2id$"))
        self.assertTrue(PasswordHasher().verify(account.password_hash, cmd.password))
        for stored in (event.payload, idempotency.response):
            self.assertNotIn(cmd.password, str(stored))
            self.assertNotIn("argon2", str(stored))

    def test_duplicate_email_creates_nothing(self) -> None:
        self.register(registration(email="ana@example.com"))

        with self.assertRaises(EmailAlreadyRegistered):
            self.register(registration(email=" ANA@example.com"))

        self.assertEqual(self.db.count(UserAccountRow), 1)
        self.assertEqual(self.db.count(OutboxEventRow), 1)
        self.assertEqual(self.db.count(IdempotencyKeyRow), 1)

    def test_retry_with_same_key_returns_stored_result(self) -> None:
        cmd = registration()
        first = self.register(cmd)

        second = self.register(cmd)

        self.assertTrue(second.replayed)
        self.assertEqual(replace(second, replayed=False), first)
        self.assertEqual(self.db.count(UserAccountRow), 1)
        self.assertEqual(self.db.count(OutboxEventRow), 1)

    def test_concurrent_registrations_with_same_email_create_one_account(self) -> None:
        commands = [registration(email="concurrente@example.com") for _ in range(CONCURRENCY)]

        outcomes = self._run_concurrently(commands)

        created = [outcome for outcome in outcomes if isinstance(outcome, RegistrationResult)]
        rejected = [o for o in outcomes if isinstance(o, EmailAlreadyRegistered)]
        self.assertEqual(len(created), 1)
        self.assertEqual(len(rejected), CONCURRENCY - 1)
        self.assertEqual(self.db.count(UserAccountRow), 1)
        self.assertEqual(self.db.count(OutboxEventRow), 1)

    def test_concurrent_retries_with_same_key_return_one_account(self) -> None:
        cmd = registration()

        outcomes = self._run_concurrently([cmd] * CONCURRENCY)

        self.assertTrue(all(isinstance(o, RegistrationResult) for o in outcomes), outcomes)
        user_ids = {o.user_id for o in outcomes if isinstance(o, RegistrationResult)}
        self.assertEqual(len(user_ids), 1)
        self.assertEqual(self.db.count(UserAccountRow), 1)
        self.assertEqual(self.db.count(OutboxEventRow), 1)

    def test_failure_writing_the_event_rolls_back_the_account(self) -> None:
        class BrokenOutbox(SqlAlchemyRegistrationUnitOfWork):
            def add_outbox(self, message: OutboxMessage) -> None:
                super().add_outbox(replace(message, event_type="x" * 300))

        register = self.db.register_user(lambda: BrokenOutbox(self.db.sessions))

        with self.assertRaises(DataError):
            register(registration())

        self.assertEqual(self.db.count(UserAccountRow), 0)
        self.assertEqual(self.db.count(OutboxEventRow), 0)
        self.assertEqual(self.db.count(IdempotencyKeyRow), 0)

    def _run_concurrently(self, commands: list[object]) -> list[object]:
        def attempt(cmd: object) -> object:
            try:
                return self.register(cmd)  # type: ignore[arg-type]
            except EmailAlreadyRegistered as error:
                return error

        with ThreadPoolExecutor(max_workers=len(commands)) as pool:
            return list(pool.map(attempt, commands))


class OutboxClaimTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = self.enterContext(write_database("users_outbox"))
        register = self.db.register_user()
        for _ in range(4):
            register(registration())
        self.store = SqlAlchemyOutboxStore(self.db.sessions)

    def test_concurrent_workers_claim_disjoint_batches(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            batches = list(
                pool.map(lambda worker: self.store.claim_batch(worker, 2, 30), ["a", "b"])
            )

        claimed = [event.event_id for batch in batches for event in batch]
        self.assertEqual(len(claimed), 4)
        self.assertEqual(len(set(claimed)), 4)
        self.assertEqual(self.store.claim_batch("c", 10, 30), [])

    def test_published_and_failed_events_leave_the_pending_set(self) -> None:
        first, second = self.store.claim_batch("a", 2, 30)

        self.store.mark_published(first.event_id, "a")
        self.store.mark_failed(second.event_id, "a", "TimeoutError", retry_in_seconds=600)

        with self.db.sessions() as session:
            published = session.get_one(OutboxEventRow, first.event_id)
            failed = session.get_one(OutboxEventRow, second.event_id)
        self.assertIsNotNone(published.published_at)
        self.assertIsNone(published.lease_owner)
        self.assertEqual(failed.attempts, 1)
        self.assertEqual(failed.last_error, "TimeoutError")
        self.assertIsNone(failed.published_at)
        remaining = self.store.claim_batch("b", 10, 30)
        self.assertEqual(len(remaining), 2)
        self.assertNotIn(second.event_id, {event.event_id for event in remaining})

    def test_expired_lease_can_be_reclaimed_by_another_worker(self) -> None:
        stuck = self.store.claim_batch("caido", 1, lease_seconds=0.001)
        time.sleep(0.05)

        reclaimed = self.store.claim_batch("relevo", 10, 30)

        self.assertIn(stuck[0].event_id, {event.event_id for event in reclaimed})

    def test_only_lease_owner_can_mark_event(self) -> None:
        [event] = self.store.claim_batch("a", 1, 30)

        self.store.mark_published(event.event_id, "intruso")

        with self.db.sessions() as session:
            self.assertIsNone(session.get_one(OutboxEventRow, event.event_id).published_at)


if __name__ == "__main__":
    unittest.main()
