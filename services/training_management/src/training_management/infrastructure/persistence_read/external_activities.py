"""HU031: transacción del consumidor de `actividad.importada.v1` en la base de lectura.

La fila se escribe con un solo `INSERT ... ON CONFLICT (session_id) DO UPDATE ... WHERE`:
inserta si falta y reemplaza solo si la versión entrante es mayor **y** la fila existente es
de Strava (una sesión de HU032 nunca se sobrescribe). Así dos versiones de la misma actividad
que llegan a la vez no compiten por una clave primaria: PostgreSQL las aplica en serie sobre
la fila y `RETURNING` dice si hubo cambio. La deduplicación por `event_id` queda en
`processed_event`; si otra entrega confirmó primero, el COMMIT falla por su clave primaria y
se traduce a `DuplicateDelivery`.
"""

from datetime import datetime
from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from training_management.application.external_activity import ExternalActivity
from training_management.application.ports import DuplicateDelivery
from training_management.domain.dashboard.model import Source
from training_management.infrastructure.persistence_read.models import (
    PROCESSED_EVENT_PRIMARY_KEY,
    ProcessedEventRow,
    TrainingSessionHistoryRow,
)


def _violated_constraint(error: IntegrityError) -> str | None:
    diagnostics = getattr(error.orig, "diag", None)
    return getattr(diagnostics, "constraint_name", None)


class SqlAlchemyExternalActivityUnitOfWork:
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

    def is_processed(self, event_id: UUID) -> bool:
        return self.session.get(ProcessedEventRow, event_id) is not None

    def save_external_session(self, activity: ExternalActivity, projected_at: datetime) -> bool:
        row = TrainingSessionHistoryRow
        values = {
            "session_id": activity.activity_id,
            "athlete_id": activity.user_id,
            "plan_id": None,
            "sport": activity.sport.value,
            "started_at": activity.started_at,
            "ended_at": activity.ended_at,
            "duration_seconds": activity.duration_seconds,
            "distance_meters": activity.distance_meters,
            "source": Source.STRAVA.value,
            "external_ref": activity.activity_id,
            "version": activity.version,
            "projected_at": projected_at,
        }
        statement = insert(row).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["session_id"],
            set_={key: statement.excluded[key] for key in values if key != "session_id"},
            where=(row.version < statement.excluded["version"])
            & (row.source == Source.STRAVA.value),
        )
        applied = self.session.execute(statement.returning(row.session_id)).first()
        return applied is not None

    def add_processed(
        self, event_id: UUID, event_type: str, aggregate_id: UUID, processed_at: datetime
    ) -> None:
        self.session.add(
            ProcessedEventRow(
                event_id=event_id,
                event_type=event_type,
                aggregate_id=aggregate_id,
                processed_at=processed_at,
            )
        )

    def commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            # Otra entrega del mismo evento confirmó primero.
            if _violated_constraint(error) == PROCESSED_EVENT_PRIMARY_KEY:
                raise DuplicateDelivery() from None
            raise
