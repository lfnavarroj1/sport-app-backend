"""Consultas de lectura de HU011: historial paginado por cursor y autorizaciones proyectadas."""

from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from training_management.application.ports import (
    AuthorizationView,
    HistoryQuery,
    SessionHistoryEntry,
)
from training_management.infrastructure.persistence_read.models import (
    TrainingAuthorizationViewRow,
    TrainingSessionHistoryRow,
)


class SqlAlchemyHistoryReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list(self, query: HistoryQuery) -> list[SessionHistoryEntry]:
        row = TrainingSessionHistoryRow
        statement = select(row).where(row.athlete_id == query.athlete_id)
        if query.ended_from is not None:
            statement = statement.where(row.ended_at >= query.ended_from)
        if query.ended_before is not None:
            statement = statement.where(row.ended_at < query.ended_before)
        if query.sport is not None:
            statement = statement.where(row.sport == query.sport)
        if query.after is not None:
            # Keyset: estrictamente después de la última entregada en el orden descendente.
            statement = statement.where(
                tuple_(row.ended_at, row.session_id)
                < tuple_(query.after.ended_at, query.after.session_id)
            )
        statement = statement.order_by(row.ended_at.desc(), row.session_id.desc()).limit(
            query.limit
        )
        with self._session_factory() as session:
            return [
                SessionHistoryEntry(
                    session_id=item.session_id,
                    athlete_id=item.athlete_id,
                    plan_id=item.plan_id,
                    sport=item.sport,
                    started_at=item.started_at,
                    ended_at=item.ended_at,
                    duration_seconds=item.duration_seconds,
                    version=item.version,
                )
                for item in session.scalars(statement)
            ]


class SqlAlchemyAuthorizationReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get(self, athlete_id: UUID, professional_id: UUID) -> AuthorizationView | None:
        with self._session_factory() as session:
            row = session.get(TrainingAuthorizationViewRow, (athlete_id, professional_id))
            if row is None:
                return None
            return AuthorizationView(
                athlete_id=row.athlete_id,
                professional_id=row.professional_id,
                status=row.status,
                version=row.version,
                granted_at=row.granted_at,
                revoked_at=row.revoked_at,
            )
