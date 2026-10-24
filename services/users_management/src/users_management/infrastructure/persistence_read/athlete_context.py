"""Contexto del deportista del contrato interno de ADR-041, desde la base de lectura.

Una sola consulta: la cuenta activa de `user_directory` y, si existen, las secciones
`demographic` (HU005) y `sport_habits` (HU006) proyectadas en `athlete_profile_view`. Solo se
extraen deportes, objetivo, ciudad, país y fecha de nacimiento.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session, sessionmaker

from users_management.application.errors import ReadModelUnavailable
from users_management.application.ports import AthleteContext
from users_management.domain.account.model import AccountStatus
from users_management.infrastructure.persistence_read.models import (
    AthleteProfileViewRow,
    UserDirectoryRow,
)


def _context(
    demographic: dict[str, Any] | None, sport_habits: dict[str, Any] | None
) -> AthleteContext:
    demographic = demographic or {}
    sport_habits = sport_habits or {}
    return AthleteContext(
        # La sección ya está en el orden canónico del catálogo (HU006).
        sports=tuple(item["sport"] for item in sport_habits.get("sports", ())),
        primary_goal=sport_habits.get("primary_goal"),
        city=demographic.get("city"),
        country_code=demographic.get("country_code"),
        birth_date=demographic.get("birth_date"),
    )


class SqlAlchemyAthleteContextReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def find(self, user_id: UUID) -> AthleteContext | None:
        try:
            with self._session_factory() as session:
                row = session.execute(
                    select(AthleteProfileViewRow.demographic, AthleteProfileViewRow.sport_habits)
                    .select_from(UserDirectoryRow)
                    .outerjoin(
                        AthleteProfileViewRow,
                        AthleteProfileViewRow.user_id == UserDirectoryRow.user_id,
                    )
                    .where(
                        UserDirectoryRow.user_id == user_id,
                        UserDirectoryRow.status == AccountStatus.ACTIVE.value,
                    )
                ).one_or_none()
        except (OperationalError, InterfaceError, PoolTimeoutError):
            # Sin detalles del motor: el contrato responde `503 upstream_unavailable`.
            raise ReadModelUnavailable() from None
        if row is None:
            return None
        return _context(row[0], row[1])
