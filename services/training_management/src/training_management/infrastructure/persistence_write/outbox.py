"""Acceso de `entrenamientos-outbox-publisher` a `outbox_event` (CMP-002, CMP-003).

Cada operación usa una transacción corta: reclamar un lote con
`FOR UPDATE SKIP LOCKED` y arrendamiento, y después marcar cada evento. La
publicación ocurre fuera de cualquier transacción.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

_CLAIM_BATCH = text(
    """
    WITH candidates AS (
        SELECT event_id
        FROM outbox_event
        WHERE published_at IS NULL
          AND next_attempt_at <= now()
          AND (lease_until IS NULL OR lease_until < now())
        ORDER BY occurred_at
        FOR UPDATE SKIP LOCKED
        LIMIT :batch_size
    )
    UPDATE outbox_event AS event
    SET lease_owner = :worker_id,
        lease_until = now() + make_interval(secs => :lease_seconds)
    FROM candidates
    WHERE event.event_id = candidates.event_id
    RETURNING event.event_id, event.event_type, event.schema_version, event.aggregate_type,
              event.aggregate_id, event.aggregate_version, event.occurred_at,
              event.correlation_id, event.causation_id, event.payload, event.attempts
    """
)

_MARK_PUBLISHED = text(
    """
    UPDATE outbox_event
    SET published_at = now(), lease_owner = NULL, lease_until = NULL, last_error = NULL
    WHERE event_id = :event_id AND lease_owner = :worker_id AND published_at IS NULL
    """
)

_MARK_FAILED = text(
    """
    UPDATE outbox_event
    SET attempts = attempts + 1,
        next_attempt_at = now() + make_interval(secs => :retry_in_seconds),
        last_error = :error,
        lease_owner = NULL,
        lease_until = NULL
    WHERE event_id = :event_id AND lease_owner = :worker_id AND published_at IS NULL
    """
)


@dataclass(frozen=True)
class ClaimedEvent:
    event_id: UUID
    event_type: str
    schema_version: int
    aggregate_type: str
    aggregate_id: UUID
    aggregate_version: int
    occurred_at: datetime
    correlation_id: UUID | None
    causation_id: str | None
    payload: dict[str, Any]
    attempts: int


class SqlAlchemyOutboxStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def claim_batch(
        self, worker_id: str, batch_size: int, lease_seconds: float
    ) -> list[ClaimedEvent]:
        with self._session_factory() as session, session.begin():
            rows = session.execute(
                _CLAIM_BATCH,
                {"batch_size": batch_size, "worker_id": worker_id, "lease_seconds": lease_seconds},
            ).mappings()
            claimed = [ClaimedEvent(**row) for row in rows]
        return sorted(claimed, key=lambda event: event.occurred_at)

    def mark_published(self, event_id: UUID, worker_id: str) -> None:
        with self._session_factory() as session, session.begin():
            session.execute(_MARK_PUBLISHED, {"event_id": event_id, "worker_id": worker_id})

    def mark_failed(
        self, event_id: UUID, worker_id: str, error: str, retry_in_seconds: float
    ) -> None:
        with self._session_factory() as session, session.begin():
            session.execute(
                _MARK_FAILED,
                {
                    "event_id": event_id,
                    "worker_id": worker_id,
                    "error": error[:256],
                    "retry_in_seconds": retry_in_seconds,
                },
            )
