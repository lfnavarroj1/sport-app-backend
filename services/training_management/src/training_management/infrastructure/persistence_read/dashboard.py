"""Consultas del tablero de HU033 (ADR-043) en la base de lectura, sin vista materializada.

- Sesiones: `training_session_history` filtrada por `athlete_id` y `ended_at` en `[from, to)`
  (índice `ix_training_session_history_athlete_order`) y agrupada por deporte y semana ISO en
  la zona del `from`: `date_trunc('week', <hora local>)` da el lunes, como
  `iso_week_start`. La tabla todavía no tiene fuente ni distancia (llegan con HU031): toda fila
  es `SPORTAPP` y sin distancia.
- Indicadores: `DISTINCT ON (metric_code)` sobre `training_metric_result` filtrado por
  `measured_at` en `[from, to)` (índice `ix_training_metric_result_athlete_order`), el más
  reciente por `measured_at` y `calculation_id`.
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Date, cast, func, literal, select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session, sessionmaker

from training_management.application.errors import DashboardSourceUnavailable
from training_management.domain.dashboard.model import (
    DashboardPeriod,
    LatestMetric,
    SessionBucket,
    Source,
)
from training_management.infrastructure.persistence_read.models import (
    TrainingMetricResultRow,
    TrainingSessionHistoryRow,
)

_UNAVAILABLE = (OperationalError, InterfaceError, PoolTimeoutError)


class SqlAlchemyDashboardReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def session_buckets(self, athlete_id: UUID, period: DashboardPeriod) -> list[SessionBucket]:
        row = TrainingSessionHistoryRow
        offset_seconds = int(period.offset.total_seconds())
        local = func.timezone("UTC", row.ended_at) + func.make_interval(
            0, 0, 0, 0, 0, 0, literal(offset_seconds)
        )
        week_start = cast(func.date_trunc("week", local), Date).label("week_start")
        statement = (
            select(
                row.sport,
                week_start,
                func.count().label("sessions_count"),
                func.coalesce(func.sum(row.duration_seconds), 0).label("duration_seconds"),
            )
            .where(
                row.athlete_id == athlete_id,
                row.ended_at >= period.start,
                row.ended_at < period.end,
            )
            .group_by(row.sport, week_start)
        )
        try:
            with self._session_factory() as session:
                rows = session.execute(statement).all()
        except _UNAVAILABLE:
            # Sin detalles del motor: la fuente se informa `UNAVAILABLE`.
            raise DashboardSourceUnavailable() from None
        return [
            SessionBucket(
                sport=str(sport),
                week_start=_as_date(week),
                source=Source.SPORTAPP,
                sessions_count=int(count),
                duration_seconds=int(seconds),
            )
            for sport, week, count, seconds in rows
        ]

    def latest_metrics(self, athlete_id: UUID, period: DashboardPeriod) -> list[LatestMetric]:
        row = TrainingMetricResultRow
        statement = (
            select(row)
            .where(
                row.athlete_id == athlete_id,
                row.measured_at >= period.start,
                row.measured_at < period.end,
            )
            .distinct(row.metric_code)
            .order_by(row.metric_code, row.measured_at.desc(), row.calculation_id.desc())
        )
        try:
            with self._session_factory() as session:
                rows = session.scalars(statement).all()
        except _UNAVAILABLE:
            raise DashboardSourceUnavailable() from None
        return [
            LatestMetric(
                indicator=item.metric_code,
                value=item.result_value.quantize(_scale(item.result_scale)),
                unit=item.result_unit,
                method_code=item.method_code,
                method_version=item.method_version,
                measured_at=item.measured_at,
                disclaimer_key=item.disclaimer_key,
            )
            for item in rows
        ]


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _scale(scale: int) -> Decimal:
    return Decimal(1).scaleb(-scale)
