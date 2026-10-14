"""Catálogo de disponibilidad para el deportista (HU020, ADR-038) sobre la base única.

Una página son los servicios `PUBLISHED` ordenados por `name` y `service_id`; con
`only_available` se exige al menos un intervalo futuro que empiece en el rango. Después se
leen solo los intervalos del rango de los servicios de la página. Sin CQRS ni bloqueos.
"""

from collections import defaultdict
from uuid import UUID

from sqlalchemy import ColumnElement, and_, exists, select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from complementary_services.application.ports import AvailabilityQuery
from complementary_services.domain.catalog.offer import ServiceOffer, ServiceStatus
from complementary_services.infrastructure.persistence.models import (
    ServiceAvailabilityRow,
    ServiceRow,
)
from complementary_services.infrastructure.persistence.unit_of_work import service_offer


def _in_range(query: AvailabilityQuery) -> ColumnElement[bool]:
    starts_at = ServiceAvailabilityRow.starts_at
    return and_(
        starts_at >= query.starts_from,
        starts_at < query.starts_before,
        starts_at > query.now,
    )


class SqlAlchemyServiceCatalogReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def published_availability(self, query: AvailabilityQuery) -> list[ServiceOffer]:
        statement = select(ServiceRow).where(ServiceRow.status == ServiceStatus.PUBLISHED.value)
        if query.categories is not None:
            statement = statement.where(
                ServiceRow.category.in_(sorted(item.value for item in query.categories))
            )
        if query.modalities is not None:
            statement = statement.where(
                ServiceRow.modality.in_(sorted(item.value for item in query.modalities))
            )
        if query.only_available:
            statement = statement.where(
                exists().where(
                    ServiceAvailabilityRow.service_id == ServiceRow.service_id, _in_range(query)
                )
            )
        if query.after is not None:
            statement = statement.where(
                tuple_(ServiceRow.name, ServiceRow.service_id)
                > tuple_(query.after.name, query.after.service_id)
            )
        statement = statement.order_by(ServiceRow.name, ServiceRow.service_id).limit(query.limit)
        with self._session_factory() as session:
            rows = list(session.scalars(statement))
            intervals: defaultdict[UUID, list[ServiceAvailabilityRow]] = defaultdict(list)
            if rows:
                for interval in session.scalars(
                    select(ServiceAvailabilityRow).where(
                        ServiceAvailabilityRow.service_id.in_([row.service_id for row in rows]),
                        _in_range(query),
                    )
                ):
                    intervals[interval.service_id].append(interval)
            return [service_offer(row, intervals[row.service_id]) for row in rows]
