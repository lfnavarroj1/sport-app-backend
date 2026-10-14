"""Consultas de los servicios propios del profesional (ADR-038) sobre la base única."""

from collections import defaultdict
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from complementary_services.application.ports import OwnServicesQuery
from complementary_services.domain.catalog.offer import ServiceOffer
from complementary_services.infrastructure.persistence.models import (
    ServiceAvailabilityRow,
    ServiceRow,
)
from complementary_services.infrastructure.persistence.unit_of_work import service_offer


class SqlAlchemyServiceReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_own(self, query: OwnServicesQuery) -> list[ServiceOffer]:
        statement = select(ServiceRow).where(ServiceRow.provider_id == query.provider_id)
        if query.status is not None:
            statement = statement.where(ServiceRow.status == query.status.value)
        if query.after is not None:
            statement = statement.where(
                tuple_(ServiceRow.updated_at, ServiceRow.service_id)
                < tuple_(query.after.updated_at, query.after.service_id)
            )
        statement = statement.order_by(
            ServiceRow.updated_at.desc(), ServiceRow.service_id.desc()
        ).limit(query.limit)
        with self._session_factory() as session:
            rows = list(session.scalars(statement))
            intervals: defaultdict[UUID, list[ServiceAvailabilityRow]] = defaultdict(list)
            if rows:
                for interval in session.scalars(
                    select(ServiceAvailabilityRow).where(
                        ServiceAvailabilityRow.service_id.in_([row.service_id for row in rows])
                    )
                ):
                    intervals[interval.service_id].append(interval)
            return [service_offer(row, intervals[row.service_id]) for row in rows]

    def own_service(self, provider_id: UUID, service_id: UUID) -> ServiceOffer | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ServiceRow).where(
                    ServiceRow.service_id == service_id, ServiceRow.provider_id == provider_id
                )
            )
            if row is None:
                return None
            intervals = session.scalars(
                select(ServiceAvailabilityRow).where(
                    ServiceAvailabilityRow.service_id == service_id
                )
            )
            return service_offer(row, intervals)
