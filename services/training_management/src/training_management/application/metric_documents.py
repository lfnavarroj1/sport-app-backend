"""Representación del cálculo de HU034 en respuestas, en el resultado idempotente guardado y
en el historial.

Es el cuerpo enviado más `athlete_id`, `result`, `status`, `calculated_at`,
`disclaimer_key` y `version` (ADR-030): el payload de `indicador.calculado.v1` con la
versión del agregado. Instantes en UTC `Z`; valores como números JSON.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from training_management.domain.metrics.events import calculated_payload
from training_management.domain.metrics.model import (
    CalculationStatus,
    MetricInput,
    MetricResult,
    TrainingMetricCalculation,
)


def calculation_document(calculation: TrainingMetricCalculation) -> dict[str, Any]:
    return {**calculated_payload(calculation), "version": calculation.version}


def calculation_from_document(document: dict[str, Any]) -> TrainingMetricCalculation:
    """Reconstruye un documento que el propio servicio guardó (`command_idempotency`)."""
    result = document["result"]
    return TrainingMetricCalculation(
        calculation_id=UUID(document["calculation_id"]),
        athlete_id=UUID(document["athlete_id"]),
        metric_code=document["metric_code"],
        method_code=document["method_code"],
        method_version=int(document["method_version"]),
        measured_at=datetime.fromisoformat(document["measured_at"]),
        calculated_at=datetime.fromisoformat(document["calculated_at"]),
        inputs=tuple(
            MetricInput(code=item["code"], value=Decimal(str(item["value"])), unit=item["unit"])
            for item in document["inputs"]
        ),
        result=MetricResult(
            value=Decimal(str(result["value"])), unit=result["unit"], scale=int(result["scale"])
        ),
        disclaimer_key=document["disclaimer_key"],
        status=CalculationStatus(document["status"]),
        version=int(document["version"]),
    )
