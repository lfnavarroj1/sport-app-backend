"""Asociaciones propias del deportista (HU017, ADR-038) sobre la base única."""

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from complementary_services.application.ports import (
    AssociatedService,
    AssociationView,
    OwnAssociationsQuery,
)
from complementary_services.infrastructure.persistence.models import (
    ServiceAssociationRow,
    ServiceRow,
)
from complementary_services.infrastructure.persistence.unit_of_work import (
    service_association,
    service_offer,
)


class SqlAlchemyAssociationReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_own(self, query: OwnAssociationsQuery) -> list[AssociationView]:
        statement = (
            select(ServiceAssociationRow, ServiceRow)
            .join(ServiceRow, ServiceRow.service_id == ServiceAssociationRow.service_id)
            .where(ServiceAssociationRow.athlete_id == query.athlete_id)
        )
        if query.status is not None:
            statement = statement.where(ServiceAssociationRow.status == query.status.value)
        if query.after is not None:
            statement = statement.where(
                tuple_(ServiceAssociationRow.associated_at, ServiceAssociationRow.association_id)
                < tuple_(query.after.associated_at, query.after.association_id)
            )
        statement = statement.order_by(
            ServiceAssociationRow.associated_at.desc(),
            ServiceAssociationRow.association_id.desc(),
        ).limit(query.limit)
        with self._session_factory() as session:
            return [
                AssociationView(
                    service_association(association),
                    AssociatedService.of(service_offer(service, ())),
                )
                for association, service in session.execute(statement).tuples()
            ]
