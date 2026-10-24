"""Lectura del perfil propio (`athlete_profile_view`) para `GET /v1/me/profile` (HU005).

Desde HU009 incluye `nutrition_habits` (ADR-035).
"""

from uuid import UUID

from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session, sessionmaker

from users_management.application.errors import ReadModelUnavailable
from users_management.application.ports import AthleteProfileView
from users_management.infrastructure.persistence_read.models import AthleteProfileViewRow


class SqlAlchemyAthleteProfileReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get(self, user_id: UUID) -> AthleteProfileView | None:
        try:
            with self._session_factory() as session:
                row = session.get(AthleteProfileViewRow, user_id)
                if row is None:
                    return None
                return AthleteProfileView(
                    user_id=row.user_id,
                    demographic=row.demographic,
                    physiological=row.physiological,
                    sport_habits=row.sport_habits,
                    nutrition_habits=row.nutrition_habits,
                    demographic_version=row.demographic_version,
                    physiological_version=row.physiological_version,
                    sport_habits_version=row.sport_habits_version,
                    nutrition_habits_version=row.nutrition_habits_version,
                    projected_at=row.projected_at,
                )
        except (OperationalError, InterfaceError, PoolTimeoutError):
            # Sin detalles del motor: la consulta se degrada a `503 upstream_unavailable`.
            raise ReadModelUnavailable() from None
