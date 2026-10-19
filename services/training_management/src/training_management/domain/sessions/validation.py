"""Errores de validación de las sesiones de entrenamiento (ADR-029).

`field` usa el nombre del campo (o `Idempotency-Key`) e `issue` pertenece al catálogo
cerrado de HU032.
"""

from dataclasses import dataclass
from enum import StrEnum


class Issue(StrEnum):
    REQUIRED = "required"
    FIELD_NOT_ALLOWED = "field_not_allowed"
    UNKNOWN_VALUE = "unknown_value"
    INVALID_UUID = "invalid_uuid"
    INVALID_DATETIME = "invalid_datetime"
    TOO_FAR_IN_FUTURE = "too_far_in_future"
    TOO_OLD = "too_old"
    ENDS_BEFORE_STARTS = "ends_before_starts"


@dataclass(frozen=True)
class Violation:
    field: str | None
    issue: Issue


class InvalidTrainingSession(Exception):
    """La solicitud incumple ADR-029; informa todas las reglas incumplidas."""

    code = "validation_error"

    def __init__(self, violations: list[Violation]) -> None:
        super().__init__(", ".join(f"{item.field}: {item.issue}" for item in violations))
        self.violations = tuple(violations)
