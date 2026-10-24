"""Transacción de `usuarios-read-projector` sobre la base de lectura (CMP-001).

`processed_event` se registra con `INSERT … ON CONFLICT DO NOTHING` dentro de la misma
transacción que el cambio de la vista: dos entregas simultáneas del mismo `event_id`
se serializan en esa inserción y solo una aplica el efecto. Se bloquea únicamente la
fila del usuario afectado.
"""

from datetime import datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from users_management.application.projections.users_projection import (
    DirectoryEmailInUse,
    DirectoryEntry,
)
from users_management.domain.profile.events import ProfileSection
from users_management.infrastructure.persistence_read.models import (
    DIRECTORY_EMAIL_UNIQUE_CONSTRAINT,
    AthleteProfileViewRow,
    ProcessedEventRow,
    UserDirectoryRow,
)


class SqlAlchemyProjectionUnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None

    @property
    def session(self) -> Session:
        if self._session is None:
            raise RuntimeError("La unidad de trabajo no está abierta.")
        return self._session

    def __enter__(self) -> Self:
        self._session = self._session_factory()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.session.rollback()
        self.session.close()
        self._session = None

    def claim_event(
        self,
        event_id: UUID,
        projection_name: str,
        aggregate_id: UUID,
        aggregate_version: int,
        processed_at: datetime,
    ) -> bool:
        claimed = self.session.scalar(
            insert(ProcessedEventRow)
            .values(
                event_id=event_id,
                projection_name=projection_name,
                aggregate_id=aggregate_id,
                aggregate_version=aggregate_version,
                processed_at=processed_at,
            )
            .on_conflict_do_nothing(index_elements=["event_id"])
            .returning(ProcessedEventRow.event_id)
        )
        return claimed is not None

    def directory_version(self, user_id: UUID) -> int | None:
        version: int | None = self.session.scalar(
            select(UserDirectoryRow.aggregate_version)
            .where(UserDirectoryRow.user_id == user_id)
            .with_for_update()
        )
        return version

    def add_directory_entry(self, entry: DirectoryEntry, projected_at: datetime) -> bool:
        # Solo el conflicto de `user_id` se absorbe: un correo repetido de otro usuario es
        # una incoherencia que debe fallar (el mensaje no se confirma).
        inserted = self.session.scalar(
            insert(UserDirectoryRow)
            .values(
                user_id=entry.user_id,
                full_name=entry.full_name,
                email=entry.email,
                actor_type=entry.actor_type,
                status=entry.status,
                aggregate_version=entry.aggregate_version,
                projected_at=projected_at,
            )
            .on_conflict_do_nothing(index_elements=["user_id"])
            .returning(UserDirectoryRow.user_id)
        )
        return inserted is not None

    def update_directory_entry(self, entry: DirectoryEntry, projected_at: datetime) -> None:
        # La fila ya está bloqueada por `directory_version`; solo se reemplaza su estado.
        try:
            self.session.execute(
                update(UserDirectoryRow)
                .where(UserDirectoryRow.user_id == entry.user_id)
                .values(
                    full_name=entry.full_name,
                    email=entry.email,
                    actor_type=entry.actor_type,
                    status=entry.status,
                    aggregate_version=entry.aggregate_version,
                    projected_at=projected_at,
                )
            )
        except IntegrityError as error:
            diagnostics = getattr(error.orig, "diag", None)
            if getattr(diagnostics, "constraint_name", None) == DIRECTORY_EMAIL_UNIQUE_CONSTRAINT:
                # Sin la sentencia ni sus parámetros (correo): la transacción se revierte.
                raise DirectoryEmailInUse() from None
            raise

    def profile_section_version(self, user_id: UUID, section: ProfileSection) -> int | None:
        column = getattr(AthleteProfileViewRow, f"{section.value}_version")
        version: int | None = self.session.scalar(
            select(column).where(AthleteProfileViewRow.user_id == user_id).with_for_update()
        )
        return version

    def save_profile_section(
        self,
        user_id: UUID,
        section: ProfileSection,
        version: int,
        document: dict[str, Any],
        projected_at: datetime,
    ) -> bool:
        document_key, version_key = section.value, f"{section.value}_version"
        statement = insert(AthleteProfileViewRow).values(
            {
                "user_id": user_id,
                document_key: document,
                version_key: version,
                "projected_at": projected_at,
            }
        )
        current = getattr(AthleteProfileViewRow, version_key)
        # Si la fila apareció entre el bloqueo y la inserción (alta simultánea de otra
        # sección), el UPSERT espera a esa transacción y vuelve a comparar la versión.
        applied = self.session.scalar(
            statement.on_conflict_do_update(
                index_elements=["user_id"],
                set_={
                    document_key: statement.excluded[document_key],
                    version_key: statement.excluded[version_key],
                    "projected_at": statement.excluded.projected_at,
                },
                where=or_(current.is_(None), current < statement.excluded[version_key]),
            ).returning(AthleteProfileViewRow.user_id)
        )
        return applied is not None

    def commit(self) -> None:
        self.session.commit()
