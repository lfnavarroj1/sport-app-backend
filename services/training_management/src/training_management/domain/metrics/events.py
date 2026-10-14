"""Evento `indicador.calculado.v1` en `sportapp.entrenamientos.v1` (ADR-030).

- Agregado `training_metric_calculation`; `aggregate_id` y clave de orden: `calculation_id`.
- Versión de agregado 1 (el cálculo es inmutable); `causation_id` = `Idempotency-Key`.
- El payload lleva el estado completo: método, versión, entradas normalizadas, resultado,
  fechas en UTC `Z`, `status: CONFIRMED` y `disclaimer_key`. Los valores son números
  JSON (entero con escala 0; con su escala decimal en otro caso).
"""

from typing import Any

from training_management.domain.metrics.catalog import INPUT_SCALE
from training_management.domain.metrics.model import (
    TrainingMetricCalculation,
    json_number,
    utc_instant,
)

EVENT_TYPE = "indicador.calculado.v1"
SCHEMA_VERSION = 1
AGGREGATE_TYPE = "training_metric_calculation"


def calculated_payload(calculation: TrainingMetricCalculation) -> dict[str, Any]:
    return {
        "calculation_id": str(calculation.calculation_id),
        "athlete_id": str(calculation.athlete_id),
        "metric_code": calculation.metric_code,
        "method_code": calculation.method_code,
        "method_version": calculation.method_version,
        "measured_at": utc_instant(calculation.measured_at),
        "calculated_at": utc_instant(calculation.calculated_at),
        "inputs": [
            {"code": item.code, "value": json_number(item.value, INPUT_SCALE), "unit": item.unit}
            for item in calculation.inputs
        ],
        "result": {
            "value": json_number(calculation.result.value, calculation.result.scale),
            "unit": calculation.result.unit,
            "scale": calculation.result.scale,
        },
        "status": calculation.status.value,
        "disclaimer_key": calculation.disclaimer_key,
    }
