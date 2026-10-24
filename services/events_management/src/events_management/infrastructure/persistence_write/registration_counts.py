"""HU025 (ADR-040): inscripciones por evento leídas de la base de escritura.

`GET /v1/me/events` y el detalle del propietario las usan para advertir que cambiar la
tarifa no modifica las inscripciones existentes. El conteo no se proyecta en `event_read`
(riesgo de clave caliente, CMP-004).
"""

from collections.abc import Collection
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from events_management.infrastructure.persistence_write.models import EventRegistrationRow


class SqlAlchemyRegistrationCounter:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def counts(self, event_ids: Collection[UUID]) -> dict[UUID, int]:
        if not event_ids:
            return {}
        registration = EventRegistrationRow
        statement = (
            select(registration.event_id, func.count())
            .where(registration.event_id.in_(list(event_ids)))
            .group_by(registration.event_id)
        )
        with self._session_factory() as session:
            return {event_id: int(count) for event_id, count in session.execute(statement)}
