"""Errores de validación del reporte de salud (ADR-037).

`field` usa la ruta del campo (`body_area`, `consent.version`, `Idempotency-Key`…) e `issue`
reutiliza las causas de ADR-029 (formas, identificadores e instantes) y de ADR-034
(`unknown_value` para un valor fuera de catálogo y `unsupported_consent_version`), porque
ADR-037 no define un catálogo propio.
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
    OUT_OF_RANGE = "out_of_range"
    UNSUPPORTED_CONSENT_VERSION = "unsupported_consent_version"


@dataclass(frozen=True)
class Violation:
    field: str | None
    issue: Issue


class InvalidHealthReport(Exception):
    """La solicitud incumple ADR-037; informa todas las reglas incumplidas."""

    code = "validation_error"

    def __init__(self, violations: list[Violation]) -> None:
        super().__init__(", ".join(f"{item.field}: {item.issue}" for item in violations))
        self.violations = tuple(violations)
