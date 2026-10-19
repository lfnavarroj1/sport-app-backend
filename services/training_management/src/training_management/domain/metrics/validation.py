"""Errores de validación del cálculo de indicadores (ADR-030).

`field` usa la ruta del campo (`metric_code`, `inputs[0].value`, `Idempotency-Key`…) e
`issue` pertenece al catálogo cerrado de ADR-030. `invalid_uuid` se agrega para
`calculation_id` e `Idempotency-Key` con la misma semántica de ADR-029, porque ADR-030
no define una causa para un identificador mal formado.
"""

from dataclasses import dataclass
from enum import StrEnum


class Issue(StrEnum):
    REQUIRED = "required"
    FIELD_NOT_ALLOWED = "field_not_allowed"
    UNKNOWN_METRIC = "unknown_metric"
    UNSUPPORTED_METHOD_VERSION = "unsupported_method_version"
    UNKNOWN_INPUT = "unknown_input"
    DUPLICATE_INPUT = "duplicate_input"
    INVALID_UNIT = "invalid_unit"
    OUT_OF_RANGE = "out_of_range"
    INVALID_DATETIME = "invalid_datetime"
    TOO_FAR_IN_FUTURE = "too_far_in_future"
    TOO_OLD = "too_old"
    INVALID_UUID = "invalid_uuid"


@dataclass(frozen=True)
class Violation:
    field: str | None
    issue: Issue


class InvalidMetricCalculation(Exception):
    """La solicitud incumple ADR-030; informa todas las reglas incumplidas."""

    code = "validation_error"

    def __init__(self, violations: list[Violation]) -> None:
        super().__init__(", ".join(f"{item.field}: {item.issue}" for item in violations))
        self.violations = tuple(violations)
