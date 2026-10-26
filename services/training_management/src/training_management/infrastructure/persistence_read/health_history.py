"""Consulta de lectura de HU012: historial de reportes de salud paginado sobre
`health_report_view` (índice `athlete_id`, `occurred_at` desc, `report_id` desc de HU042)."""

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from training_management.application.ports import (
    HealthReportHistoryEntry,
    HealthReportHistoryQuery,
)
from training_management.infrastructure.persistence_read.models import HealthReportViewRow


class SqlAlchemyHealthReportHistoryReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list(self, query: HealthReportHistoryQuery) -> list[HealthReportHistoryEntry]:
        row = HealthReportViewRow
        statement = select(row).where(row.athlete_id == query.athlete_id)
        if query.occurred_from is not None:
            statement = statement.where(row.occurred_at >= query.occurred_from)
        if query.occurred_before is not None:
            statement = statement.where(row.occurred_at < query.occurred_before)
        if query.type is not None:
            statement = statement.where(row.type == query.type)
        if query.severity is not None:
            statement = statement.where(row.severity == query.severity)
        if query.after is not None:
            # Keyset: estrictamente después del último entregado en el orden descendente.
            statement = statement.where(
                tuple_(row.occurred_at, row.report_id)
                < tuple_(query.after.occurred_at, query.after.report_id)
            )
        statement = statement.order_by(row.occurred_at.desc(), row.report_id.desc()).limit(
            query.limit
        )
        with self._session_factory() as session:
            return [
                HealthReportHistoryEntry(
                    report_id=item.report_id,
                    athlete_id=item.athlete_id,
                    type=item.type,
                    body_area=item.body_area,
                    severity=item.severity,
                    occurred_at=item.occurred_at,
                    expected_recovery_days=item.expected_recovery_days,
                    status=item.status,
                    session_id=item.session_id,
                )
                for item in session.scalars(statement)
            ]
