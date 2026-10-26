"""Consultas de lectura de HU011 (historial paginado, autorizaciones proyectadas y deportistas
vigentes del profesional), HU032 (sesión activa) y HU035 (sesiones planificadas del
calendario)."""

from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from training_management.application.ports import (
    ActiveSessionView,
    AuthorizationView,
    AuthorizedAthleteEntry,
    AuthorizedAthletesQuery,
    HistoryQuery,
    PlannedSessionEntry,
    PlannedSessionQuery,
    SessionHistoryEntry,
)
from training_management.infrastructure.persistence_read.models import (
    ActiveTrainingSessionRow,
    TrainingAuthorizationViewRow,
    TrainingPlanSessionViewRow,
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
                    source=item.source,
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


class SqlAlchemyAuthorizedAthleteReader:
    """HU011 (CA5): autorizaciones vigentes del profesional, paginadas por keyset."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list(self, query: AuthorizedAthletesQuery) -> list[AuthorizedAthleteEntry]:
        row = TrainingAuthorizationViewRow
        statement = select(row.athlete_id, row.granted_at).where(
            row.professional_id == query.professional_id, row.status == "ACTIVE"
        )
        if query.after is not None:
            # Keyset: estrictamente después del último entregado en el orden descendente.
            statement = statement.where(
                tuple_(row.granted_at, row.athlete_id)
                < tuple_(query.after.authorized_at, query.after.athlete_id)
            )
        statement = statement.order_by(row.granted_at.desc(), row.athlete_id.desc()).limit(
            query.limit
        )
        with self._session_factory() as session:
            return [
                AuthorizedAthleteEntry(athlete_id=athlete_id, authorized_at=granted_at)
                for athlete_id, granted_at in session.execute(statement)
            ]


class SqlAlchemyActiveSessionReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def current(self, athlete_id: UUID) -> ActiveSessionView | None:
        # Sesiones distintas tienen claves de orden distintas: el inicio de una nueva puede
        # proyectarse antes que el fin de la anterior. Se devuelve la más reciente.
        row = ActiveTrainingSessionRow
        statement = (
            select(row)
            .where(row.athlete_id == athlete_id)
            .order_by(row.started_at.desc(), row.session_id.desc())
            .limit(1)
        )
        with self._session_factory() as session:
            item = session.scalar(statement)
            if item is None:
                return None
            return ActiveSessionView(
                session_id=item.session_id,
                athlete_id=item.athlete_id,
                plan_id=item.plan_id,
                sport=item.sport,
                started_at=item.started_at,
                version=item.version,
            )


class SqlAlchemyPlannedSessionReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list(self, query: PlannedSessionQuery) -> list[PlannedSessionEntry]:
        row = TrainingPlanSessionViewRow
        statement = select(row).where(
            row.athlete_id == query.athlete_id,
            row.plan_status == "ASSIGNED",
            row.scheduled_date >= query.date_from,
            row.scheduled_date <= query.date_to,
        )
        if query.after is not None:
            # Keyset: estrictamente después de la última entregada en el orden ascendente.
            statement = statement.where(
                tuple_(row.scheduled_date, row.plan_id, row.session_id)
                > tuple_(query.after.scheduled_date, query.after.plan_id, query.after.session_id)
            )
        statement = statement.order_by(row.scheduled_date, row.plan_id, row.session_id).limit(
            query.limit
        )
        with self._session_factory() as session:
            return [
                PlannedSessionEntry(
                    session_id=item.session_id,
                    plan_id=item.plan_id,
                    athlete_id=item.athlete_id,
                    plan_title=item.plan_title,
                    scheduled_date=item.scheduled_date,
                    sport=item.sport,
                    duration_minutes=item.duration_minutes,
                    intensity_rpe=item.intensity_rpe,
                )
                for item in session.scalars(statement)
            ]
