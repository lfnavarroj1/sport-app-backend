"""Reclamo de corridas por `terceros-sync-worker` (HU031, ADR-042 §8).

Una transacción corta: toma la corrida `QUEUED` más antigua (o una `RUNNING` cuyo
arrendamiento venció: su worker murió) con `FOR UPDATE SKIP LOCKED`, la pasa a `RUNNING`
con este worker y un arrendamiento, y confirma antes de llamar a Strava.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from third_party_management.domain.synchronization import SyncRun
from third_party_management.infrastructure.persistence.models import SyncRunRow
from third_party_management.infrastructure.persistence.unit_of_work import sync_run

_CLAIM = text(
    """
    WITH candidate AS (
        SELECT sync_run_id
        FROM sync_run
        WHERE status = 'QUEUED'
           OR (status = 'RUNNING' AND (lease_until IS NULL OR lease_until < now()))
        ORDER BY requested_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    UPDATE sync_run AS run
    SET status = 'RUNNING',
        started_at = COALESCE(run.started_at, now()),
        worker_id = :worker_id,
        lease_until = now() + make_interval(secs => :lease_seconds)
    FROM candidate
    WHERE run.sync_run_id = candidate.sync_run_id
    RETURNING run.sync_run_id
    """
)


class SqlAlchemySyncRunClaimer:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def claim(self, worker_id: str, lease_seconds: float) -> SyncRun | None:
        with self._session_factory() as session, session.begin():
            sync_run_id = session.execute(
                _CLAIM, {"worker_id": worker_id, "lease_seconds": lease_seconds}
            ).scalar_one_or_none()
            if sync_run_id is None:
                return None
            row = session.get(SyncRunRow, sync_run_id)
            return sync_run(row) if row is not None else None
