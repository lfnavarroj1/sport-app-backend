"""Reclamo de corridas de `terceros-sync-worker` contra PostgreSQL real (HU031, ADR-042 §8).

`FOR UPDATE SKIP LOCKED`: dos workers nunca obtienen la misma corrida; una `RUNNING` con el
arrendamiento vencido (su worker murió) se vuelve a reclamar y una vigente no.
"""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text, update
from support import ThirdPartyDatabase, third_party_database

from third_party_management.domain.synchronization import SyncRun, SyncStatus
from third_party_management.infrastructure.persistence.models import SyncRunRow
from third_party_management.infrastructure.persistence.sync_claims import (
    SqlAlchemySyncRunClaimer,
)

LEASE_SECONDS = 300.0


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class SyncRunClaimTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: ThirdPartyDatabase = self.enterContext(third_party_database("third_party_claim"))
        self.claimer = SqlAlchemySyncRunClaimer(self.db.sessions)

    def queue(self, requested_at: datetime | None = None) -> SyncRun:
        run = SyncRun.queue(uuid4(), requested_at or now(), uuid4())
        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.add_run(run)
            unit_of_work.commit()
        return run

    def set_running(self, sync_run_id: UUID, lease_until: datetime | None) -> None:
        with self.db.engine.begin() as connection:
            connection.execute(
                update(SyncRunRow)
                .where(SyncRunRow.sync_run_id == sync_run_id)
                .values(
                    status="RUNNING",
                    worker_id="dead-worker",
                    started_at=now() - timedelta(minutes=20),
                    lease_until=lease_until,
                )
            )

    def test_claims_the_oldest_queued_run_with_a_lease(self) -> None:
        older = self.queue(now() - timedelta(minutes=5))
        self.queue()

        claimed = self.claimer.claim("worker-a", LEASE_SECONDS)

        assert claimed is not None
        self.assertEqual(claimed.sync_run_id, older.sync_run_id)
        self.assertEqual(claimed.status, SyncStatus.RUNNING)
        self.assertIsNotNone(claimed.started_at)
        row = self.db.row(SyncRunRow, SyncRunRow.sync_run_id == older.sync_run_id)
        self.assertEqual(row.worker_id, "worker-a")
        self.assertGreater(row.lease_until, datetime.now(UTC) + timedelta(seconds=200))

    def test_nothing_to_claim(self) -> None:
        self.assertIsNone(self.claimer.claim("worker-a", LEASE_SECONDS))

    def test_a_locked_run_is_skipped(self) -> None:
        locked = self.queue(now() - timedelta(minutes=5))
        free = self.queue()

        with self.db.engine.connect() as other:
            other.execute(
                text("SELECT sync_run_id FROM sync_run WHERE sync_run_id = :id FOR UPDATE"),
                {"id": locked.sync_run_id},
            )
            claimed = self.claimer.claim("worker-b", LEASE_SECONDS)
            nothing = self.claimer.claim("worker-c", LEASE_SECONDS)
            other.rollback()

        assert claimed is not None
        self.assertEqual(claimed.sync_run_id, free.sync_run_id)
        self.assertIsNone(nothing, "la corrida bloqueada se salta sin esperar")

    def test_concurrent_workers_never_claim_the_same_run(self) -> None:
        runs = {self.queue(now() - timedelta(seconds=index)).sync_run_id for index in range(4)}
        workers = 8
        barrier = threading.Barrier(workers)

        def claim(index: int) -> UUID | None:
            barrier.wait(timeout=30)
            claimed = self.claimer.claim(f"worker-{index}", LEASE_SECONDS)
            return claimed.sync_run_id if claimed is not None else None

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(claim, range(workers)))

        obtained = [result for result in results if result is not None]
        self.assertEqual(len(obtained), len(set(obtained)), "ninguna corrida se repite")
        self.assertEqual(set(obtained), runs)
        self.assertEqual(results.count(None), workers - len(runs))

    def test_running_run_with_expired_lease_is_reclaimed(self) -> None:
        run = self.queue()
        self.set_running(run.sync_run_id, now() - timedelta(minutes=1))

        claimed = self.claimer.claim("worker-new", LEASE_SECONDS)

        assert claimed is not None
        self.assertEqual(claimed.sync_run_id, run.sync_run_id)
        row = self.db.row(SyncRunRow, SyncRunRow.sync_run_id == run.sync_run_id)
        self.assertEqual(row.worker_id, "worker-new")
        # Conserva el inicio original de la corrida.
        self.assertLess(row.started_at, datetime.now(UTC) - timedelta(minutes=10))

    def test_running_run_without_lease_is_reclaimed(self) -> None:
        run = self.queue()
        self.set_running(run.sync_run_id, None)

        claimed = self.claimer.claim("worker-new", LEASE_SECONDS)

        assert claimed is not None
        self.assertEqual(claimed.sync_run_id, run.sync_run_id)

    def test_running_run_with_valid_lease_is_not_reclaimed(self) -> None:
        run = self.queue()
        self.set_running(run.sync_run_id, now() + timedelta(minutes=5))

        self.assertIsNone(self.claimer.claim("worker-new", LEASE_SECONDS))
        row = self.db.row(SyncRunRow, SyncRunRow.sync_run_id == run.sync_run_id)
        self.assertEqual(row.worker_id, "dead-worker")

    def test_finished_runs_are_never_claimed(self) -> None:
        run = self.queue()
        with self.db.unit_of_work() as unit_of_work:
            unit_of_work.save_run(run.finish(None, 0, now()))
            unit_of_work.commit()

        self.assertIsNone(self.claimer.claim("worker-a", LEASE_SECONDS))


if __name__ == "__main__":
    unittest.main()
