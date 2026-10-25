"""Citas propias del deportista o de los servicios del profesional (HU040, ADR-046) sobre la
base única; sin proyección ni bloqueos."""

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from complementary_services.application.ports import (
    AppointmentView,
    AssociatedService,
    OwnAppointmentsQuery,
)
from complementary_services.infrastructure.persistence.models import AppointmentRow, ServiceRow
from complementary_services.infrastructure.persistence.unit_of_work import (
    appointment_of,
    service_offer,
)


class SqlAlchemyAppointmentReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_own(self, query: OwnAppointmentsQuery) -> list[AppointmentView]:
        statement = select(AppointmentRow, ServiceRow).join(
            ServiceRow, ServiceRow.service_id == AppointmentRow.service_id
        )
        if query.athlete_id is not None:
            statement = statement.where(AppointmentRow.athlete_id == query.athlete_id)
        elif query.provider_id is not None:
            statement = statement.where(AppointmentRow.provider_id == query.provider_id)
        else:
            raise ValueError("La consulta necesita al deportista o al profesional.")
        if query.starts_from is not None:
            statement = statement.where(AppointmentRow.starts_at >= query.starts_from)
        if query.starts_before is not None:
            statement = statement.where(AppointmentRow.starts_at < query.starts_before)
        if query.status is not None:
            statement = statement.where(AppointmentRow.status == query.status.value)
        if query.after is not None:
            statement = statement.where(
                tuple_(AppointmentRow.starts_at, AppointmentRow.appointment_id)
                > tuple_(query.after.starts_at, query.after.appointment_id)
            )
        statement = statement.order_by(
            AppointmentRow.starts_at, AppointmentRow.appointment_id
        ).limit(query.limit)
        with self._session_factory() as session:
            return [
                AppointmentView(
                    appointment_of(appointment), AssociatedService.of(service_offer(service, ()))
                )
                for appointment, service in session.execute(statement).tuples()
            ]
