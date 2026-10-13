"""Forma estable de los errores de validación del evento (ADR-028 §9).

`field` usa rutas de propiedades (`location.country_code`) e `issue` pertenece al
catálogo cerrado de HU021.
"""

from dataclasses import dataclass
from enum import StrEnum


class Issue(StrEnum):
    REQUIRED = "required"
    FIELD_NOT_ALLOWED = "field_not_allowed"
    INVALID_LENGTH = "invalid_length"
    UNKNOWN_VALUE = "unknown_value"
    NOT_IN_FUTURE = "not_in_future"
    OUT_OF_RANGE = "out_of_range"
    TIME_ZONE_UNKNOWN = "time_zone_unknown"
    TIME_ZONE_OFFSET_MISMATCH = "time_zone_offset_mismatch"


@dataclass(frozen=True)
class Violation:
    field: str | None
    issue: Issue


class InvalidSportEvent(Exception):
    """El evento incumple ADR-028; informa todas las reglas incumplidas."""

    code = "validation_error"

    def __init__(self, violations: list[Violation]) -> None:
        super().__init__(", ".join(f"{item.field}: {item.issue}" for item in violations))
        self.violations = tuple(violations)
