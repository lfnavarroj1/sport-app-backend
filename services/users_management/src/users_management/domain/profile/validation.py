"""Forma estable de los errores de validación del perfil (ADR-034).

`field` es la ruta de la propiedad (`consent.version`, `blood_pressure.systolic_mmhg`)
e `issue` pertenece a un catálogo cerrado. Ninguna violación contiene el valor recibido:
los datos demográficos y de salud no deben aparecer en errores ni en logs.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TypeVar


class Issue(StrEnum):
    REQUIRED = "required"
    FIELD_NOT_ALLOWED = "field_not_allowed"
    INVALID_LENGTH = "invalid_length"
    UNKNOWN_VALUE = "unknown_value"
    OUT_OF_RANGE = "out_of_range"
    TOO_MANY_DECIMALS = "too_many_decimals"
    INVALID_DATE = "invalid_date"
    INVALID_DATETIME = "invalid_datetime"
    TOO_FAR_IN_FUTURE = "too_far_in_future"
    TOO_OLD = "too_old"
    DUPLICATE_VALUE = "duplicate_value"
    TOO_MANY_ITEMS = "too_many_items"
    INCONSISTENT_VALUES = "inconsistent_values"
    UNSUPPORTED_CONSENT_VERSION = "unsupported_consent_version"


@dataclass(frozen=True)
class Violation:
    field: str | None
    issue: Issue


class InvalidProfileSection(Exception):
    """La sección incumple ADR-034; informa todas las reglas incumplidas (todo o nada)."""

    code = "validation_error"

    def __init__(self, violations: Iterable[Violation]) -> None:
        self.violations = tuple(violations)
        super().__init__(", ".join(f"{item.field}: {item.issue}" for item in self.violations))


EnumT = TypeVar("EnumT", bound=StrEnum)

# Un decimal se escribe como cadena (ADR-027): signo opcional, dígitos y decimales.
_DECIMAL_TEXT = re.compile(r"^-?\d+(\.\d+)?$")
_ONE_DECIMAL = Decimal("0.1")


class Collector:
    """Reúne las violaciones de una sección.

    El transporte ya informó los campos con un tipo JSON incorrecto o no permitidos; un
    campo así llega como `None` y no se vuelve a informar como `required`.
    """

    def __init__(self, transport_violations: Iterable[Violation]) -> None:
        self.violations: list[Violation] = list(transport_violations)
        self._flagged = {item.field for item in self.violations}

    def add(self, field: str | None, issue: Issue) -> None:
        self.violations.append(Violation(field, issue))

    def _already_reported(self, field: str) -> bool:
        if None in self._flagged:
            return True  # El cuerpo completo es inválido.
        return any(
            flagged is not None and (field == flagged or field.startswith(f"{flagged}."))
            for flagged in self._flagged
        )

    def required(self, value: object, field: str) -> bool:
        """`True` si hay valor; si falta, lo informa salvo que ya se haya informado."""
        if value is not None:
            return True
        if not self._already_reported(field):
            self.add(field, Issue.REQUIRED)
        return False

    def raise_if_any(self) -> None:
        if self.violations:
            raise InvalidProfileSection(self.violations)

    def catalog(
        self, value: str | None, field: str, catalog: Callable[[str], EnumT]
    ) -> EnumT | None:
        if value is None:
            return None
        try:
            return catalog(value)
        except ValueError:
            self.add(field, Issue.UNKNOWN_VALUE)
            return None

    def text(self, value: str | None, field: str, minimum: int, maximum: int) -> str | None:
        """Recorta los espacios exteriores y comprueba la longitud."""
        if value is None:
            return None
        trimmed = value.strip()
        if not minimum <= len(trimmed) <= maximum:
            self.add(field, Issue.INVALID_LENGTH)
            return None
        return trimmed

    def integer(self, value: int | None, field: str, minimum: int, maximum: int) -> int | None:
        if value is None:
            return None
        if not minimum <= value <= maximum:
            self.add(field, Issue.OUT_OF_RANGE)
            return None
        return value

    def decimal(
        self, value: str | None, field: str, minimum: Decimal, maximum: Decimal
    ) -> Decimal | None:
        """Cadena decimal con 0 o 1 decimal; se normaliza a un decimal (`"68"` → `68.0`)."""
        if value is None:
            return None
        if not _DECIMAL_TEXT.match(value):
            self.add(field, Issue.UNKNOWN_VALUE)
            return None
        number = Decimal(value)  # La expresión regular garantiza un decimal finito.
        exponent = number.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -1:
            self.add(field, Issue.TOO_MANY_DECIMALS)
            return None
        if not minimum <= number <= maximum:
            self.add(field, Issue.OUT_OF_RANGE)
            return None
        return number.quantize(_ONE_DECIMAL)

    def expected_version(self, value: int | None) -> int | None:
        if value is not None and value < 1:
            self.add("expected_version", Issue.OUT_OF_RANGE)
            return None
        return value


def format_instant(value: datetime) -> str:
    """Instante en UTC con sufijo `Z` (el frontend convierte a la zona del usuario)."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def format_decimal(value: Decimal) -> str:
    """Decimal de la sección como cadena con exactamente un decimal."""
    return format(value.quantize(_ONE_DECIMAL), "f")
