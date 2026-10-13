"""Forma estable de los errores de validación de la oferta (ADR-027 §14).

`field` usa rutas de propiedades con índices entre corchetes (`availability[0].time_zone`)
e `issue` pertenece a un catálogo cerrado.
"""

from dataclasses import dataclass
from enum import StrEnum


class Issue(StrEnum):
    REQUIRED = "required"
    FIELD_NOT_ALLOWED = "field_not_allowed"
    UNSUPPORTED_CURRENCY = "unsupported_currency"
    OUT_OF_RANGE = "out_of_range"
    TOO_MANY_DECIMALS = "too_many_decimals"
    INVALID_LENGTH = "invalid_length"
    UNKNOWN_VALUE = "unknown_value"
    NOT_IN_FUTURE = "not_in_future"
    ENDS_BEFORE_STARTS = "ends_before_starts"
    OVERLAPPING_INTERVAL = "overlapping_interval"
    TIME_ZONE_UNKNOWN = "time_zone_unknown"
    TIME_ZONE_OFFSET_MISMATCH = "time_zone_offset_mismatch"
    TOO_MANY_INTERVALS = "too_many_intervals"


@dataclass(frozen=True)
class Violation:
    field: str | None
    issue: Issue


class InvalidServiceOffer(Exception):
    """La oferta incumple ADR-027; informa todas las reglas incumplidas."""

    code = "validation_error"

    def __init__(self, violations: list[Violation]) -> None:
        super().__init__(", ".join(f"{item.field}: {item.issue}" for item in violations))
        self.violations = tuple(violations)
