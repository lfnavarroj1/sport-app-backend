"""Base de lectura de HU034: filas de `training_metric_result` y consulta paginada propia."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from training_management.application.ports import MetricResultQuery
from training_management.domain.metrics.catalog import INPUT_SCALE
from training_management.domain.metrics.model import (
    CalculationStatus,
    MetricInput,
    MetricResult,
    TrainingMetricCalculation,
    json_number,
)
from training_management.infrastructure.persistence_read.models import TrainingMetricResultRow


def metric_result_values(
    calculation: TrainingMetricCalculation, projected_at: datetime
) -> dict[str, Any]:
    return {
        "calculation_id": calculation.calculation_id,
        "athlete_id": calculation.athlete_id,
        "metric_code": calculation.metric_code,
        "method_code": calculation.method_code,
        "method_version": calculation.method_version,
        "measured_at": calculation.measured_at,
        "calculated_at": calculation.calculated_at,
        "inputs": [
            {"code": item.code, "value": json_number(item.value, INPUT_SCALE), "unit": item.unit}
            for item in calculation.inputs
        ],
        "result_value": calculation.result.value,
        "result_unit": calculation.result.unit,
        "result_scale": calculation.result.scale,
        "status": calculation.status.value,
        "disclaimer_key": calculation.disclaimer_key,
        "version": calculation.version,
        "projected_at": projected_at,
    }


def metric_result(row: TrainingMetricResultRow) -> TrainingMetricCalculation:
    return TrainingMetricCalculation(
        calculation_id=row.calculation_id,
        athlete_id=row.athlete_id,
        metric_code=row.metric_code,
        method_code=row.method_code,
        method_version=row.method_version,
        measured_at=row.measured_at,
        calculated_at=row.calculated_at,
        inputs=tuple(
            MetricInput(code=item["code"], value=Decimal(str(item["value"])), unit=item["unit"])
            for item in row.inputs
        ),
        result=MetricResult(value=row.result_value, unit=row.result_unit, scale=row.result_scale),
        disclaimer_key=row.disclaimer_key,
        status=CalculationStatus(row.status),
        version=row.version,
    )


class SqlAlchemyMetricResultReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list(self, query: MetricResultQuery) -> list[TrainingMetricCalculation]:
        row = TrainingMetricResultRow
        statement = select(row).where(row.athlete_id == query.athlete_id)
        if query.metric_code is not None:
            statement = statement.where(row.metric_code == query.metric_code)
        if query.after is not None:
            # Keyset: estrictamente después del último entregado en el orden descendente.
            statement = statement.where(
                tuple_(row.measured_at, row.calculation_id)
                < tuple_(query.after.measured_at, query.after.calculation_id)
            )
        statement = statement.order_by(row.measured_at.desc(), row.calculation_id.desc()).limit(
            query.limit
        )
        with self._session_factory() as session:
            return [metric_result(item) for item in session.scalars(statement)]
