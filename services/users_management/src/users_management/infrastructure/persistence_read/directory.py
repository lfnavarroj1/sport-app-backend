"""Lectura del directorio de identidad (`user_directory`) para HU002, HU010, HU011 y HU014."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session, sessionmaker

from users_management.application.errors import ReadModelUnavailable
from users_management.application.ports import AccountView, UserDisplay, UserReference
from users_management.domain.account.model import AccountStatus, ActorType
from users_management.infrastructure.persistence_read.models import UserDirectoryRow


class SqlAlchemyUserDirectory:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def find_active_by_email(self, email: str) -> UserReference | None:
        with self._session_factory() as session:
            row = session.execute(
                select(UserDirectoryRow.user_id, UserDirectoryRow.actor_type).where(
                    UserDirectoryRow.email == email,
                    UserDirectoryRow.status == AccountStatus.ACTIVE.value,
                )
            ).one_or_none()
        if row is None:
            return None
        return UserReference(user_id=row[0], actor_type=ActorType(row[1]))

    def find_active_by_ids(self, user_ids: tuple[UUID, ...]) -> list[UserDisplay]:
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    UserDirectoryRow.user_id, UserDirectoryRow.full_name, UserDirectoryRow.email
                ).where(
                    UserDirectoryRow.user_id.in_(user_ids),
                    UserDirectoryRow.status == AccountStatus.ACTIVE.value,
                    UserDirectoryRow.full_name.is_not(None),
                )
            ).all()
        found = {
            user_id: (full_name, email)
            for user_id, full_name, email in rows
            if full_name is not None
        }
        # Mismo orden que la solicitud: el BFF compone la página en el orden de Entrenamientos.
        return [
            UserDisplay(user_id=user_id, full_name=found[user_id][0], email=found[user_id][1])
            for user_id in user_ids
            if user_id in found
        ]


class SqlAlchemyAccountReader:
    """HU002: la cuenta propia tal como la proyectó `usuarios-read-projector`."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get(self, user_id: UUID) -> AccountView | None:
        try:
            with self._session_factory() as session:
                row = session.get(UserDirectoryRow, user_id)
                if row is None or row.full_name is None:
                    return None
                return AccountView(
                    user_id=row.user_id,
                    full_name=row.full_name,
                    email=row.email,
                    actor_type=row.actor_type,
                    status=row.status,
                    version=row.aggregate_version,
                    projected_at=row.projected_at,
                )
        except (OperationalError, InterfaceError, PoolTimeoutError):
            # Sin detalles del motor: la consulta se degrada a `503 upstream_unavailable`.
            raise ReadModelUnavailable() from None
