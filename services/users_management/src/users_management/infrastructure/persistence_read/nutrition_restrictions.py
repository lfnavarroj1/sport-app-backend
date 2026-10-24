"""Restricciones alimenticias del contrato interno de ADR-035, desde la base de lectura.

Una sola consulta: la cuenta activa de `user_directory` y, si existe, la sección
`nutrition_habits` proyectada en `athlete_profile_view` (HU009). Solo se leen el patrón,
las restricciones, las alergias y la versión: ninguna otra sección del perfil sale de aquí.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session, sessionmaker

from users_management.application.errors import ReadModelUnavailable
from users_management.application.ports import NutritionRestrictions
from users_management.domain.account.model import AccountStatus
from users_management.infrastructure.persistence_read.models import (
    AthleteProfileViewRow,
    UserDirectoryRow,
)

NO_HABITS = NutritionRestrictions(restrictions=(), allergies=(), diet_pattern=None, version=None)


def _restrictions(section: dict[str, Any] | None, version: int | None) -> NutritionRestrictions:
    if section is None or version is None:
        return NO_HABITS
    return NutritionRestrictions(
        restrictions=tuple(section["restrictions"]),
        allergies=tuple(section["allergies"]),
        diet_pattern=section["diet_pattern"],
        version=version,
    )


class SqlAlchemyNutritionRestrictionsReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def find(self, user_id: UUID) -> NutritionRestrictions | None:
        try:
            with self._session_factory() as session:
                row = session.execute(
                    select(
                        AthleteProfileViewRow.nutrition_habits,
                        AthleteProfileViewRow.nutrition_habits_version,
                    )
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
        return _restrictions(row[0], row[1])
