"""Búsqueda de deportistas del contrato interno de ADR-045 (HU039), desde la base de lectura.

Una sola consulta sobre `user_directory` (cuenta `active` de tipo `athlete`) y
`athlete_profile_view`: alguno de los deportes en `sport_habits.sports[*].sport` (contención
JSONB), la ciudad de `demographic` normalizada con la misma tabla que `normalize_term` y el
país exacto. Sin hábitos deportivos o sin perfil demográfico no hay coincidencia. Orden
`user_id` ascendente con keyset; se lee una fila de más para saber si hay otra página.
Solo se devuelven identificadores.
"""

from typing import Any

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session, sessionmaker

from users_management.application.errors import ReadModelUnavailable
from users_management.application.ports import AthleteSearchCriteria, AthleteSearchPage
from users_management.domain.account.model import AccountStatus, ActorType
from users_management.domain.profile.terms import ACCENTED, PLAIN
from users_management.infrastructure.persistence_read.models import (
    AthleteProfileViewRow,
    UserDirectoryRow,
)


def _normalized(column: Any) -> ColumnElement[str]:
    """`normalize_term` en SQL: recorta, aplica la tabla de equivalencias y luego minúsculas."""
    result: ColumnElement[str] = func.lower(func.translate(func.btrim(column), ACCENTED, PLAIN))
    return result


class SqlAlchemyAthleteSearchReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def search(self, criteria: AthleteSearchCriteria) -> AthleteSearchPage:
        profile = AthleteProfileViewRow
        directory = UserDirectoryRow
        statement = (
            select(directory.user_id)
            .join(profile, profile.user_id == directory.user_id)
            .where(
                directory.status == AccountStatus.ACTIVE.value,
                directory.actor_type == ActorType.ATHLETE.value,
                profile.demographic["country_code"].astext == criteria.country_code,
                _normalized(profile.demographic["city"].astext) == criteria.city,
                or_(
                    *(
                        profile.sport_habits["sports"].contains([{"sport": sport}])
                        for sport in criteria.sports
                    )
                ),
            )
        )
        if criteria.after is not None:
            statement = statement.where(directory.user_id > criteria.after)
        statement = statement.order_by(directory.user_id).limit(criteria.limit + 1)
        try:
            with self._session_factory() as session:
                found = list(session.scalars(statement))
        except (OperationalError, InterfaceError, PoolTimeoutError):
            # Sin detalles del motor: el contrato responde `503 upstream_unavailable`.
            raise ReadModelUnavailable() from None
        return AthleteSearchPage(
            user_ids=tuple(found[: criteria.limit]), has_more=len(found) > criteria.limit
        )
