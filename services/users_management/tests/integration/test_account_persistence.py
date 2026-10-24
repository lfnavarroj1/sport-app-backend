"""HU002 (ADR-035) contra PostgreSQL real (requiere `TEST_DATABASE_URL`).

Escritura: cuenta, outbox e idempotencia en una transacción; unicidad del correo con dos
cuentas y en concurrencia; bloqueo de fila con dos ediciones simultáneas. Lectura: el
proyector aplica `usuario.actualizado.v1` sobre `user_directory` (nombre y correo) y la
consulta de la cuenta lee la proyección.
"""

import threading
import unittest
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from support import registration, write_database

from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.postgres import temporary_database
from users_management.application.commands.update_account import (
    OPERATION,
    UpdateAccount,
    UpdateAccountCommand,
)
from users_management.application.messages import parse_users_event
from users_management.application.projections.users_projection import (
    DirectoryEmailInUse,
    ProjectionOutcome,
    UsersProjector,
)
from users_management.domain.account.editing import AccountChangeInput
from users_management.domain.account.errors import (
    AccountVersionConflict,
    EmailAlreadyRegistered,
)
from users_management.infrastructure.clock import SystemClock
from users_management.infrastructure.persistence_read.directory import SqlAlchemyAccountReader
from users_management.infrastructure.persistence_read.models import UserDirectoryRow
from users_management.infrastructure.persistence_read.projection import (
    SqlAlchemyProjectionUnitOfWork,
)
from users_management.infrastructure.persistence_write.account import SqlAlchemyAccountUnitOfWork
from users_management.infrastructure.persistence_write.models import (
    IdempotencyKeyRow,
    OutboxEventRow,
    UserAccountRow,
)
from users_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from users_management.workers.outbox_publisher import build_envelope

READ_HISTORY = "users_management.infrastructure.persistence_read:migrations"


def command(**values: object) -> UpdateAccountCommand:
    change: dict[str, Any] = {
        "expected_version": 1,
        "full_name": "Ana María Pérez",
        "email": f"nuevo-{uuid4().hex[:8]}@example.com",
        **values,
    }
    return UpdateAccountCommand(change=AccountChangeInput(**change), idempotency_key=uuid4())


def concurrently(*calls: Callable[[], object]) -> list[object]:
    """Ejecuta las llamadas a la vez y devuelve su resultado o la excepción que lanzaron."""
    barrier = threading.Barrier(len(calls))
    results: list[object] = [None] * len(calls)

    def run(index: int, call: Callable[[], object]) -> None:
        barrier.wait()
        try:
            results[index] = call()
        except Exception as error:
            results[index] = error

    threads = [threading.Thread(target=run, args=item) for item in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


class AccountWriteIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.database = self.enterContext(write_database("users_hu002_write"))
        self.update = UpdateAccount(
            unit_of_work=lambda: SqlAlchemyAccountUnitOfWork(self.database.sessions),
            clock=SystemClock(),
        )

    def account(self, email: str | None = None) -> UUID:
        values = {"email": email} if email else {}
        return self.database.register_user()(registration(**values)).user_id

    def row(self, user_id: UUID) -> UserAccountRow:
        with self.database.sessions() as session:
            row = session.get(UserAccountRow, user_id)
            assert row is not None
            return row

    def outbox(self, user_id: UUID) -> list[OutboxEventRow]:
        with self.database.sessions() as session:
            return list(
                session.scalars(
                    select(OutboxEventRow)
                    .where(OutboxEventRow.aggregate_id == user_id)
                    .order_by(OutboxEventRow.aggregate_version)
                )
            )

    def test_account_outbox_and_key_are_committed_together(self) -> None:
        """CA1 y CA5: versión nueva, evento sin hash y resultado idempotente."""
        user_id = self.account()
        before = self.row(user_id)
        sent = command(email="Ana.Nueva@Example.com")

        result = self.update(user_id, sent)

        row = self.row(user_id)
        self.assertEqual(
            (row.version, row.email, row.full_name), (2, "ana.nueva@example.com", "Ana María Pérez")
        )
        self.assertEqual(row.password_hash, before.password_hash)
        self.assertEqual((row.actor_type, row.status), (before.actor_type, before.status))
        self.assertGreater(row.updated_at, before.updated_at)
        registered, updated = self.outbox(user_id)
        self.assertEqual(registered.event_type, "usuario.registrado.v1")
        self.assertEqual(
            (updated.event_type, updated.aggregate_type, updated.aggregate_version),
            ("usuario.actualizado.v1", "user_account", 2),
        )
        self.assertEqual(updated.payload, result.document)
        self.assertEqual(updated.causation_id, str(sent.idempotency_key))
        with self.database.sessions() as session:
            key = session.get(IdempotencyKeyRow, sent.idempotency_key)
        assert key is not None
        self.assertEqual((key.operation, key.resource_id), (OPERATION, user_id))

        again = self.update(user_id, sent)

        self.assertTrue(again.replayed)
        self.assertEqual(len(self.outbox(user_id)), 2, "el reintento no publica otra vez")

    def test_same_email_creates_no_version_nor_event(self) -> None:
        user_id = self.account("misma@example.com")

        result = self.update(user_id, command(full_name="Ana Pérez", email=" MISMA@example.com "))

        self.assertEqual((result.version, self.row(user_id).version), (1, 1))
        self.assertEqual(len(self.outbox(user_id)), 1)

    def test_email_of_another_account_is_rejected(self) -> None:
        """CA3: con PostgreSQL; la cuenta conserva su correo."""
        self.account("ocupado@example.com")
        user_id = self.account("propio@example.com")

        with self.assertRaises(EmailAlreadyRegistered):
            self.update(user_id, command(email="ocupado@example.com"))

        self.assertEqual(
            (self.row(user_id).email, self.row(user_id).version), ("propio@example.com", 1)
        )
        self.assertEqual(len(self.outbox(user_id)), 1)

    def test_two_accounts_claiming_the_same_email_at_once(self) -> None:
        """CA3 en concurrencia: la restricción única deja pasar solo a una cuenta."""
        first, second = self.account(), self.account()

        results = concurrently(
            lambda: self.update(first, command(email="disputado@example.com")),
            lambda: self.update(second, command(email="disputado@example.com")),
        )

        failures = [item for item in results if isinstance(item, Exception)]
        self.assertEqual(len(failures), 1, results)
        self.assertIsInstance(failures[0], EmailAlreadyRegistered)
        emails = [self.row(user_id).email for user_id in (first, second)]
        self.assertEqual(emails.count("disputado@example.com"), 1)
        self.assertEqual(len(self.outbox(first)) + len(self.outbox(second)), 3)

    def test_concurrent_edits_of_one_account_create_a_single_version(self) -> None:
        """CA4 con bloqueo de fila: la segunda edición ve la versión nueva y no sobrescribe."""
        user_id = self.account()

        results = concurrently(
            lambda: self.update(user_id, command(full_name="Primera")),
            lambda: self.update(user_id, command(full_name="Segunda")),
        )

        conflicts = [item for item in results if isinstance(item, AccountVersionConflict)]
        self.assertEqual(len(conflicts), 1, results)
        self.assertEqual(conflicts[0].current_version, 2)
        self.assertEqual(self.row(user_id).version, 2)
        self.assertEqual([event.aggregate_version for event in self.outbox(user_id)], [1, 2])

    def test_stale_version_does_not_overwrite(self) -> None:
        user_id = self.account()
        self.update(user_id, command(full_name="Vigente"))

        with self.assertRaises(AccountVersionConflict):
            self.update(user_id, command(full_name="Obsoleta"))

        self.assertEqual(self.row(user_id).full_name, "Vigente")


class AccountProjectionIntegrationTest(unittest.TestCase):
    """Registro y edición → outbox → sobre publicado → proyector → consulta (sin Pub/Sub)."""

    def setUp(self) -> None:
        self.database = self.enterContext(write_database("users_hu002_proj_write"))
        read_url = self.enterContext(temporary_database("users_hu002_proj_read"))
        upgrade(READ_HISTORY, read_url)
        read_engine = create_engine(read_url)
        self.addCleanup(read_engine.dispose)
        self.read_sessions = sessionmaker(read_engine)
        self.update = UpdateAccount(
            unit_of_work=lambda: SqlAlchemyAccountUnitOfWork(self.database.sessions),
            clock=SystemClock(),
        )
        self.projector = UsersProjector(
            unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(self.read_sessions),
            clock=SystemClock(),
        )
        self.reader = SqlAlchemyAccountReader(self.read_sessions)

    def drain(self) -> list[ProjectionOutcome | Exception]:
        """Publica la outbox pendiente en orden y la proyecta; devuelve cada resultado."""
        outcomes: list[ProjectionOutcome | Exception] = []
        store = SqlAlchemyOutboxStore(self.database.sessions)
        claimed = sorted(store.claim_batch("it-hu002", 50, 30), key=lambda event: event.occurred_at)
        for event in claimed:
            try:
                outcomes.append(self.projector(parse_users_event(build_envelope(event))))
            except DirectoryEmailInUse as error:
                outcomes.append(error)
            store.mark_published(event.event_id, "it-hu002")
        return outcomes

    def test_directory_reflects_name_and_email_once_projected(self) -> None:
        """CA1: la consulta refleja el cambio una vez proyectado (y no antes)."""
        user_id = self.database.register_user()(registration(full_name="Ana Pérez")).user_id
        self.drain()
        self.update(user_id, command(full_name="Ana María", email="ana.maria@example.com"))

        before = self.reader.get(user_id)
        self.assertEqual(self.drain(), [ProjectionOutcome.APPLIED])
        after = self.reader.get(user_id)

        assert before is not None
        assert after is not None
        self.assertEqual((before.full_name, before.version), ("Ana Pérez", 1))
        self.assertEqual(
            (after.full_name, after.email, after.version), ("Ana María", "ana.maria@example.com", 2)
        )

    def test_email_swap_waits_until_the_previous_owner_is_projected(self) -> None:
        """El correo liberado por otra cuenta: si su edición llega después, se reintenta."""
        register = self.database.register_user()
        first = register(registration(email="a@example.com")).user_id
        second = register(registration(email="b@example.com")).user_id
        self.drain()
        self.update(first, command(email="c@example.com"))
        self.update(second, command(email="a@example.com"))
        store = SqlAlchemyOutboxStore(self.database.sessions)
        claimed = {event.aggregate_id: event for event in store.claim_batch("it", 10, 30)}

        with self.assertRaises(DirectoryEmailInUse):
            self.projector(parse_users_event(build_envelope(claimed[second])))
        self.projector(parse_users_event(build_envelope(claimed[first])))
        outcome = self.projector(parse_users_event(build_envelope(claimed[second])))

        self.assertEqual(outcome, ProjectionOutcome.APPLIED)
        with self.read_sessions() as session:
            emails = dict(
                session.execute(select(UserDirectoryRow.user_id, UserDirectoryRow.email)).all()
            )
        self.assertEqual(emails, {first: "c@example.com", second: "a@example.com"})

    def test_account_not_projected_yet(self) -> None:
        self.assertIsNone(self.reader.get(uuid4()))
