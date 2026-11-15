"""`SqlAlchemyUnitOfWork` contra PostgreSQL real (HU031, ADR-042 §2-§8).

Cubre el `state` de un solo uso, la vinculación cifrada y su índice único por atleta, la
corrida pendiente única, la clave de idempotencia, la deduplicación de actividades y la
outbox en el mismo COMMIT que el agregado.
"""

import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError
from support import (
    ThirdPartyDatabase,
    linked_account,
    outbox_message,
    plain_tokens,
    sealer,
    third_party_database,
)

from third_party_management.application.errors import (
    AthleteAlreadyLinked,
    IdempotencyKeyConflict,
    SyncRunConflict,
)
from third_party_management.application.ports import IdempotencyRecord, OAuthState
from third_party_management.domain.activities import ImportedActivity
from third_party_management.domain.connections import AccountStatus
from third_party_management.domain.synchronization import (
    SyncErrorCode,
    SyncRun,
    SyncStatus,
)
from third_party_management.infrastructure.persistence.models import (
    ExternalAccountRow,
    IdempotencyKeyRow,
    ImportedActivityRow,
    OAuthStateRow,
    OutboxEventRow,
    SyncRunRow,
)

OPERATION = "request_strava_sync"


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class _DatabaseCase(unittest.TestCase):
    prefix = "third_party_uow"

    def setUp(self) -> None:
        self.db: ThirdPartyDatabase = self.enterContext(third_party_database(self.prefix))


class OAuthStatePersistenceTest(_DatabaseCase):
    prefix = "third_party_state"

    def add_state(self, user_id: object, created_at: datetime) -> str:
        state = uuid4().hex
        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.add_oauth_state(OAuthState(state, user_id, created_at))  # type: ignore[arg-type]
            unit_of_work.commit()
        return state

    def test_state_is_single_use(self) -> None:
        user_id, created = uuid4(), now()
        state = self.add_state(user_id, created)

        with self.db.unit_of_work() as unit_of_work:
            taken = unit_of_work.take_oauth_state(state)
            unit_of_work.commit()
        with self.db.unit_of_work() as unit_of_work:
            again = unit_of_work.take_oauth_state(state)
            unit_of_work.commit()

        self.assertIsNotNone(taken)
        assert taken is not None
        self.assertEqual((taken.state, taken.user_id), (state, user_id))
        self.assertEqual(taken.created_at, created)
        self.assertIsNone(again)
        self.assertEqual(self.db.count(OAuthStateRow), 0)

    def test_take_without_commit_is_discarded(self) -> None:
        state = self.add_state(uuid4(), now())

        with self.db.unit_of_work() as unit_of_work:
            self.assertIsNotNone(unit_of_work.take_oauth_state(state))

        self.assertEqual(self.db.count(OAuthStateRow, OAuthStateRow.state == state), 1)

    def test_delete_states_with_cutoff_keeps_recent_ones_and_other_users(self) -> None:
        user_id, other = uuid4(), uuid4()
        old = self.add_state(user_id, now() - timedelta(minutes=30))
        recent = self.add_state(user_id, now())
        foreign = self.add_state(other, now() - timedelta(minutes=30))

        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.delete_oauth_states(user_id, now() - timedelta(minutes=10))
            unit_of_work.commit()

        self.assertEqual(self.db.count(OAuthStateRow, OAuthStateRow.state == old), 0)
        self.assertEqual(self.db.count(OAuthStateRow, OAuthStateRow.state == recent), 1)
        self.assertEqual(self.db.count(OAuthStateRow, OAuthStateRow.state == foreign), 1)

    def test_delete_states_without_cutoff_removes_all_of_the_user(self) -> None:
        user_id, other = uuid4(), uuid4()
        self.add_state(user_id, now() - timedelta(minutes=30))
        self.add_state(user_id, now())
        foreign = self.add_state(other, now())

        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.delete_oauth_states(user_id, None)
            unit_of_work.commit()

        self.assertEqual(self.db.count(OAuthStateRow, OAuthStateRow.user_id == user_id), 0)
        self.assertEqual(self.db.count(OAuthStateRow, OAuthStateRow.state == foreign), 1)


class ExternalAccountPersistenceTest(_DatabaseCase):
    prefix = "third_party_account"

    def test_saves_and_reads_the_encrypted_link(self) -> None:
        user_id, cipher, tokens = uuid4(), sealer(), plain_tokens()
        account = linked_account(user_id, "777", cipher.seal(user_id, tokens))

        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.save_account(account)
            unit_of_work.commit()
        with self.db.unit_of_work() as unit_of_work:
            stored = unit_of_work.get_account(user_id)

        self.assertEqual(stored, account)
        assert stored is not None
        assert stored.tokens is not None
        self.assertEqual(cipher.open(user_id, stored.tokens), tokens)
        self.assertTrue(stored.is_active)

    def test_tokens_are_stored_only_as_ciphertext(self) -> None:
        user_id, tokens = uuid4(), plain_tokens()
        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.save_account(
                linked_account(user_id, "778", sealer().seal(user_id, tokens))
            )
            unit_of_work.commit()

        with self.db.engine.connect() as connection:
            row = connection.execute(
                text("SELECT * FROM external_account WHERE user_id = :user_id"),
                {"user_id": user_id},
            ).one()
        plain = (tokens.access_token, tokens.refresh_token)
        for column, value in row._mapping.items():
            raw = bytes(value) if isinstance(value, bytes | memoryview) else str(value).encode()
            for secret in plain:
                with self.subTest(column=column):
                    self.assertNotIn(secret.encode(), raw)
        self.assertIsNotNone(row._mapping["access_token_enc"])
        self.assertIsNotNone(row._mapping["refresh_token_enc"])

    def test_for_update_locks_the_row(self) -> None:
        user_id = uuid4()
        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.save_account(
                linked_account(user_id, "779", sealer().seal(user_id, plain_tokens()))
            )
            unit_of_work.commit()

        with self.db.unit_of_work() as unit_of_work:
            locked = unit_of_work.get_account(user_id, for_update=True)
            with self.db.engine.connect() as other:
                visible = other.execute(
                    text(
                        "SELECT user_id FROM external_account WHERE user_id = :user_id "
                        "FOR UPDATE SKIP LOCKED"
                    ),
                    {"user_id": user_id},
                ).all()
                other.rollback()

        self.assertIsNotNone(locked)
        self.assertEqual(visible, [], "otra transacción no obtiene la fila bloqueada")

    def test_active_owner_of_only_reports_active_links(self) -> None:
        user_id, cipher = uuid4(), sealer()
        account = linked_account(user_id, "780", cipher.seal(user_id, plain_tokens()))
        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.save_account(account)
            unit_of_work.commit()
        with self.db.unit_of_work() as unit_of_work:
            owner = unit_of_work.active_owner_of("780")
            unknown = unit_of_work.active_owner_of("999")

        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.save_account(account.revoke(now()))
            unit_of_work.commit()
        with self.db.unit_of_work() as unit_of_work:
            after_revoke = unit_of_work.active_owner_of("780")
            revoked = unit_of_work.get_account(user_id)

        self.assertEqual(owner, user_id)
        self.assertIsNone(unknown)
        self.assertIsNone(after_revoke)
        assert revoked is not None
        self.assertEqual(revoked.status, AccountStatus.REVOKED)
        self.assertIsNone(revoked.tokens)
        self.assertEqual(revoked.version, account.version + 1)

    def test_relink_replaces_the_row_with_the_next_version(self) -> None:
        user_id, cipher = uuid4(), sealer()
        first = linked_account(user_id, "781", cipher.seal(user_id, plain_tokens()))
        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.save_account(first)
            unit_of_work.commit()

        second = linked_account(user_id, "781", cipher.seal(user_id, plain_tokens()), current=first)
        with self.db.unit_of_work() as unit_of_work:
            current = unit_of_work.get_account(user_id, for_update=True)
            self.assertEqual(current, first)
            unit_of_work.save_account(second)
            unit_of_work.commit()

        self.assertEqual(self.db.count(ExternalAccountRow), 1)
        stored = self.db.row(ExternalAccountRow, ExternalAccountRow.user_id == user_id)
        self.assertEqual(stored.version, 2)
        self.assertEqual(bytes(stored.access_token_enc), second.tokens.access)  # type: ignore[union-attr]

    def test_same_strava_athlete_active_for_two_users_is_rejected(self) -> None:
        first, second, cipher = uuid4(), uuid4(), sealer()
        account = linked_account(first, "782", cipher.seal(first, plain_tokens()))
        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.save_account(account)
            unit_of_work.commit()

        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.save_account(
                linked_account(second, "782", cipher.seal(second, plain_tokens()))
            )
            with self.assertRaises(AthleteAlreadyLinked):
                unit_of_work.commit()

        self.assertIsNone(self.db.row(ExternalAccountRow, ExternalAccountRow.user_id == second))

        # Revocada la primera, el atleta de Strava puede vincularse a otro deportista.
        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.save_account(account.revoke(now()))
            unit_of_work.commit()
        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.save_account(
                linked_account(second, "782", cipher.seal(second, plain_tokens()))
            )
            unit_of_work.commit()
        with self.db.unit_of_work() as unit_of_work:
            self.assertEqual(unit_of_work.active_owner_of("782"), second)


class SyncRunPersistenceTest(_DatabaseCase):
    prefix = "third_party_runs"

    def queue(self, user_id: object, requested_at: datetime | None = None) -> SyncRun:
        run = SyncRun.queue(user_id, requested_at or now(), uuid4())  # type: ignore[arg-type]
        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.add_run(run)
            unit_of_work.commit()
        return run

    def test_only_one_pending_run_per_athlete(self) -> None:
        user_id = uuid4()
        first = self.queue(user_id)

        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.add_run(SyncRun.queue(user_id, now(), None))
            with self.assertRaises(SyncRunConflict):
                unit_of_work.commit()

        # Otro deportista no se ve afectado.
        self.queue(uuid4())
        # Terminada la primera, se acepta una nueva corrida.
        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.save_run(first.finish(None, 2, now()))
            unit_of_work.commit()
        self.queue(user_id)

        self.assertEqual(self.db.count(SyncRunRow, SyncRunRow.user_id == user_id), 2)

    def test_latest_and_pending_runs(self) -> None:
        user_id = uuid4()
        old = self.queue(user_id, now() - timedelta(hours=1))
        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.save_run(old.finish(SyncErrorCode.RATE_LIMITED, 0, now()))
            unit_of_work.commit()
        recent = self.queue(user_id)

        with self.db.unit_of_work() as unit_of_work:
            latest = unit_of_work.latest_run(user_id)
            pending = unit_of_work.pending_runs(user_id)
            finished = unit_of_work.get_run(old.sync_run_id)
            nobody = unit_of_work.latest_run(uuid4())

        self.assertEqual(latest, recent)
        self.assertEqual(pending, [recent])
        assert finished is not None
        self.assertEqual(finished.status, SyncStatus.RATE_LIMITED)
        self.assertEqual(finished.error_code, SyncErrorCode.RATE_LIMITED)
        self.assertIsNotNone(finished.finished_at)
        self.assertIsNone(nobody)

    def test_lock_owned_run_only_for_the_running_worker(self) -> None:
        run = self.queue(uuid4())
        queued = self.queue(uuid4())
        with self.db.engine.begin() as connection:
            connection.execute(
                update(SyncRunRow)
                .where(SyncRunRow.sync_run_id == run.sync_run_id)
                .values(
                    status="RUNNING",
                    worker_id="worker-a",
                    started_at=now(),
                    lease_until=now() + timedelta(minutes=5),
                )
            )

        with self.db.unit_of_work() as unit_of_work:
            owned = unit_of_work.lock_owned_run(run.sync_run_id, "worker-a")
            foreign = unit_of_work.lock_owned_run(run.sync_run_id, "worker-b")
            not_running = unit_of_work.lock_owned_run(queued.sync_run_id, "worker-a")

        assert owned is not None
        self.assertEqual(owned.status, SyncStatus.RUNNING)
        self.assertIsNone(foreign)
        self.assertIsNone(not_running)

    def test_finishing_a_run_clears_its_lease(self) -> None:
        run = self.queue(uuid4())
        with self.db.engine.begin() as connection:
            connection.execute(
                update(SyncRunRow)
                .where(SyncRunRow.sync_run_id == run.sync_run_id)
                .values(status="RUNNING", worker_id="worker-a", lease_until=now())
            )

        with self.db.unit_of_work() as unit_of_work:
            running = unit_of_work.lock_owned_run(run.sync_run_id, "worker-a")
            assert running is not None
            unit_of_work.save_run(running.cancel(now()))
            unit_of_work.commit()

        stored = self.db.row(SyncRunRow, SyncRunRow.sync_run_id == run.sync_run_id)
        self.assertEqual((stored.status, stored.error_code), ("FAILED", "integration_revoked"))
        self.assertIsNone(stored.lease_until)


class IdempotencyPersistenceTest(_DatabaseCase):
    prefix = "third_party_idem"

    def test_key_is_unique_per_user_operation_and_key(self) -> None:
        user_id, key = uuid4(), uuid4()
        record = IdempotencyRecord(user_id, OPERATION, key, uuid4(), now())
        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.add_idempotency(record)
            unit_of_work.commit()

        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.add_idempotency(IdempotencyRecord(user_id, OPERATION, key, uuid4(), now()))
            with self.assertRaises(IdempotencyKeyConflict):
                unit_of_work.commit()

        with self.db.unit_of_work() as unit_of_work:
            found = unit_of_work.find_idempotency(user_id, OPERATION, key)
            unit_of_work.add_idempotency(IdempotencyRecord(uuid4(), OPERATION, key, uuid4(), now()))
            unit_of_work.commit()

        self.assertEqual(found, record)
        self.assertEqual(self.db.count(IdempotencyKeyRow), 2)

    def test_delete_idempotency(self) -> None:
        user_id, key = uuid4(), uuid4()
        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.add_idempotency(IdempotencyRecord(user_id, OPERATION, key, uuid4(), now()))
            unit_of_work.commit()

        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.delete_idempotency(user_id, OPERATION, key)
            unit_of_work.commit()
        with self.db.unit_of_work() as unit_of_work:
            self.assertIsNone(unit_of_work.find_idempotency(user_id, OPERATION, key))

    def test_conflicting_run_discards_the_key_of_the_same_transaction(self) -> None:
        user_id = uuid4()
        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.add_run(SyncRun.queue(user_id, now(), None))
            unit_of_work.commit()

        with self.db.unit_of_work() as unit_of_work:
            run = SyncRun.queue(user_id, now(), None)
            unit_of_work.add_run(run)
            unit_of_work.add_idempotency(
                IdempotencyRecord(user_id, OPERATION, uuid4(), run.sync_run_id, now())
            )
            with self.assertRaises(SyncRunConflict):
                unit_of_work.commit()

        self.assertEqual(self.db.count(IdempotencyKeyRow), 0)


def imported(external_id: str, user_id: object, started_at: datetime) -> ImportedActivity:
    instant = now()
    return ImportedActivity(
        external_activity_id=external_id,
        user_id=user_id,  # type: ignore[arg-type]
        activity_id=uuid4(),
        external_version=f"{external_id}:3600:10000.4",
        payload_hash=uuid4().hex,
        version=1,
        started_at=started_at,
        imported_at=instant,
        updated_at=instant,
    )


class ImportedActivityPersistenceTest(_DatabaseCase):
    prefix = "third_party_activity"

    def test_new_change_and_last_imported_start(self) -> None:
        user_id = uuid4()
        early = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
        late = datetime(2026, 10, 21, 7, 30, tzinfo=UTC)
        first = imported("1001", user_id, early)
        with self.db.unit_of_work() as unit_of_work:
            self.assertIsNone(unit_of_work.last_imported_start(user_id))
            self.assertIsNone(unit_of_work.get_imported("1001"))
            unit_of_work.save_imported(first, new=True)
            # La misma transacción ya la encuentra (actividad repetida en una corrida).
            self.assertEqual(unit_of_work.get_imported("1001"), first)
            unit_of_work.save_imported(imported("1002", user_id, late), new=True)
            unit_of_work.commit()

        changed = ImportedActivity(
            external_activity_id="1001",
            user_id=user_id,
            activity_id=first.activity_id,
            external_version="1001:3700:10100.0",
            payload_hash=uuid4().hex,
            version=2,
            started_at=early,
            imported_at=first.imported_at,
            updated_at=now(),
        )
        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.save_imported(changed, new=False)
            unit_of_work.commit()

        with self.db.unit_of_work() as unit_of_work:
            stored = unit_of_work.get_imported("1001")
            latest = unit_of_work.last_imported_start(user_id)
            other = unit_of_work.last_imported_start(uuid4())

        self.assertEqual(stored, changed)
        self.assertEqual(latest, late)
        self.assertIsNone(other)

    def test_provider_and_external_id_are_unique(self) -> None:
        user_id = uuid4()
        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.save_imported(imported("2001", user_id, now()), new=True)
            unit_of_work.commit()

        with self.db.unit_of_work() as unit_of_work, self.assertRaises(IntegrityError):
            unit_of_work.save_imported(imported("2001", uuid4(), now()), new=True)

        self.assertEqual(self.db.count(ImportedActivityRow), 1)

    def test_outbox_is_committed_with_the_activity(self) -> None:
        user_id = uuid4()
        activity = imported("3001", user_id, now())
        message = outbox_message(activity.activity_id)
        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.save_imported(activity, new=True)
            unit_of_work.add_outbox(message)
            unit_of_work.commit()

        stored = self.db.row(OutboxEventRow, OutboxEventRow.event_id == message.event_id)
        self.assertEqual(self.db.count(ImportedActivityRow), 1)
        self.assertEqual(stored.aggregate_id, activity.activity_id)
        self.assertEqual(stored.payload, message.payload)
        self.assertEqual(stored.attempts, 0)
        self.assertIsNone(stored.published_at)
        self.assertEqual(stored.next_attempt_at, message.occurred_at)

    def test_without_commit_neither_activity_nor_outbox_persist(self) -> None:
        user_id = uuid4()
        activity = imported("4001", user_id, now())
        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.save_imported(activity, new=True)
            unit_of_work.add_outbox(outbox_message(activity.activity_id))

        self.assertEqual(self.db.count(ImportedActivityRow), 0)
        self.assertEqual(self.db.count(OutboxEventRow), 0)

    def test_failed_commit_discards_the_outbox(self) -> None:
        user_id = uuid4()
        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.add_run(SyncRun.queue(user_id, now(), None))
            unit_of_work.commit()

        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.add_run(SyncRun.queue(user_id, now(), None))
            unit_of_work.add_outbox(outbox_message())
            with self.assertRaises(SyncRunConflict):
                unit_of_work.commit()

        self.assertEqual(self.db.count(OutboxEventRow), 0)


if __name__ == "__main__":
    unittest.main()
