"""Lectura del directorio de identidad (`user_directory`) para HU010."""

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from users_management.application.ports import UserReference
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
