"""Cálculo inmutable de un indicador de HU034 (ADR-030).

- El móvil genera `calculation_id` (UUID v4) e informa `measured_at` (fin de la prueba)
  en UTC `AAAA-MM-DDTHH:MM:SSZ`; se rechaza más de 5 minutos por delante del reloj del
  backend o más de 365 días de antigüedad. `calculated_at` lo asigna el backend.
- Las entradas se validan contra el método del catálogo inyectado y se normalizan en el
  orden del catálogo; un valor no entero o no numérico es `out_of_range`.
- El resultado se calcula con `Decimal` y queda `CONFIRMED` en la versión 1; no se
  modifica ni se borra.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from training_management.domain.metrics.catalog import (
    InputDefinition,
    MethodDefinition,
    MetricCatalog,
    quantum,
)
from training_management.domain.metrics.validation import (
    InvalidMetricCalculation,
    Issue,
    Violation,
)

FUTURE_TOLERANCE = timedelta(minutes=5)
MAX_AGE = timedelta(days=365)
CALCULATED_VERSION = 1
UUID_VERSION_4 = 4
REQUEST_FIELDS = (
    "calculation_id",
    "metric_code",
    "method_code",
    "method_version",
    "measured_at",
    "inputs",
)
INPUT_FIELDS = ("code", "value", "unit")
_UTC_SECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class CalculationStatus(StrEnum):
    CONFIRMED = "CONFIRMED"


@dataclass(frozen=True)
class MetricInput:
    code: str
    value: Decimal
    unit: str


@dataclass(frozen=True)
class MetricResult:
    value: Decimal
    unit: str
    scale: int


def utc_instant(value: datetime) -> str:
    """ISO 8601 en UTC con sufijo `Z`; la conversión a la zona del usuario es del cliente."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def plain_decimal(value: Decimal) -> str:
    """Representación canónica sin exponente ni ceros sobrantes (`250.0000` → `250`)."""
    return format(value.normalize(), "f")


def json_number(value: Decimal, scale: int) -> int | float:
    """Número JSON con la escala indicada: entero con escala 0 (`238`), si no decimal (`42.4`).

    Solo serializa un `Decimal` ya calculado y redondeado; el cálculo nunca usa `float`.
    """
    quantized = value.quantize(quantum(scale))
    return int(quantized) if scale == 0 else float(quantized)


@dataclass(frozen=True)
class CalculationData:
    """Solicitud validada y normalizada; base de la idempotencia por `calculation_id`."""

    calculation_id: UUID
    metric_code: str
    method_code: str
    method_version: int
    measured_at: datetime
    inputs: tuple[MetricInput, ...]

    @property
    def fingerprint(self) -> str:
        """Huella de los datos del cálculo (sin el identificador ni la clave)."""
        canonical = json.dumps(
            [
                self.metric_code,
                self.method_code,
                self.method_version,
                utc_instant(self.measured_at),
                [[item.code, plain_decimal(item.value), item.unit] for item in self.inputs],
            ],
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ValidatedCalculation:
    data: CalculationData
    method: MethodDefinition


@dataclass(frozen=True)
class TrainingMetricCalculation:
    calculation_id: UUID
    athlete_id: UUID
    metric_code: str
    method_code: str
    method_version: int
    measured_at: datetime
    calculated_at: datetime
    inputs: tuple[MetricInput, ...]
    result: MetricResult
    disclaimer_key: str
    status: CalculationStatus = CalculationStatus.CONFIRMED
    version: int = CALCULATED_VERSION

    @classmethod
    def calculate(
        cls, *, athlete_id: UUID, validated: ValidatedCalculation, now: datetime
    ) -> "TrainingMetricCalculation":
        data, method = validated.data, validated.method
        value = method.calculate({item.code: item.value for item in data.inputs})
        return cls(
            calculation_id=data.calculation_id,
            athlete_id=athlete_id,
            metric_code=data.metric_code,
            method_code=data.method_code,
            method_version=data.method_version,
            measured_at=data.measured_at,
            calculated_at=now.astimezone(UTC).replace(microsecond=0),
            inputs=data.inputs,
            result=MetricResult(value=value, unit=method.result_unit, scale=method.result_scale),
            disclaimer_key=method.disclaimer_key,
        )

    @property
    def data(self) -> CalculationData:
        return CalculationData(
            calculation_id=self.calculation_id,
            metric_code=self.metric_code,
            method_code=self.method_code,
            method_version=self.method_version,
            measured_at=self.measured_at,
            inputs=self.inputs,
        )

    def matches(self, athlete_id: UUID, data: CalculationData) -> bool:
        """Mismo deportista y mismos datos normalizados que ya se calcularon."""
        return self.athlete_id == athlete_id and self.data.fingerprint == data.fingerprint


def _calculation_id(value: object, violations: list[Violation]) -> UUID | None:
    if value is None:
        violations.append(Violation("calculation_id", Issue.REQUIRED))
        return None
    try:
        parsed = UUID(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.version != UUID_VERSION_4:
        violations.append(Violation("calculation_id", Issue.INVALID_UUID))
        return None
    return parsed


def _metric_code(value: object, catalog: MetricCatalog, violations: list[Violation]) -> str | None:
    if value is None:
        violations.append(Violation("metric_code", Issue.REQUIRED))
        return None
    if not isinstance(value, str) or not catalog.has_metric(value):
        violations.append(Violation("metric_code", Issue.UNKNOWN_METRIC))
        return None
    return value


def _method_code(
    value: object, metric_code: str | None, catalog: MetricCatalog, violations: list[Violation]
) -> str | None:
    if value is None:
        violations.append(Violation("method_code", Issue.REQUIRED))
        return None
    # Un método que no existe o que no pertenece al indicador no es una versión admitida.
    if not isinstance(value, str) or (
        metric_code is not None and not catalog.has_method(metric_code, value)
    ):
        violations.append(Violation("method_code", Issue.UNSUPPORTED_METHOD_VERSION))
        return None
    return value


def _method(
    document: dict[str, Any], catalog: MetricCatalog, violations: list[Violation]
) -> MethodDefinition | None:
    metric_code = _metric_code(document.get("metric_code"), catalog, violations)
    method_code = _method_code(document.get("method_code"), metric_code, catalog, violations)
    version = document.get("method_version")
    if version is None:
        violations.append(Violation("method_version", Issue.REQUIRED))
        return None
    if not isinstance(version, int) or isinstance(version, bool):
        violations.append(Violation("method_version", Issue.UNSUPPORTED_METHOD_VERSION))
        return None
    if metric_code is None or method_code is None:
        return None
    method = catalog.find(metric_code, method_code, version)
    if method is None:
        violations.append(Violation("method_version", Issue.UNSUPPORTED_METHOD_VERSION))
    return method


def parse_measured_at(value: object, now: datetime, violations: list[Violation]) -> datetime | None:
    """Instante UTC `AAAA-MM-DDTHH:MM:SSZ` dentro de la ventana de ADR-030."""
    if value is None:
        violations.append(Violation("measured_at", Issue.REQUIRED))
        return None
    if not isinstance(value, str) or not _UTC_SECONDS.match(value):
        violations.append(Violation("measured_at", Issue.INVALID_DATETIME))
        return None
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        violations.append(Violation("measured_at", Issue.INVALID_DATETIME))
        return None
    if instant > now + FUTURE_TOLERANCE:
        violations.append(Violation("measured_at", Issue.TOO_FAR_IN_FUTURE))
        return None
    if instant < now - MAX_AGE:
        violations.append(Violation("measured_at", Issue.TOO_OLD))
        return None
    return instant.astimezone(UTC)


def _input(
    index: int,
    item: object,
    method: MethodDefinition | None,
    seen: set[str],
    violations: list[Violation],
) -> MetricInput | None:
    path = f"inputs[{index}]"
    if not isinstance(item, dict):
        violations.append(Violation(path, Issue.UNKNOWN_INPUT))
        return None
    before = len(violations)
    violations.extend(
        Violation(f"{path}.{name}", Issue.FIELD_NOT_ALLOWED)
        for name in item
        if name not in INPUT_FIELDS
    )
    code = item.get("code")
    definition = None
    if code is None:
        violations.append(Violation(f"{path}.code", Issue.REQUIRED))
    elif not isinstance(code, str) or (method is not None and method.input(code) is None):
        violations.append(Violation(f"{path}.code", Issue.UNKNOWN_INPUT))
    elif code in seen:
        violations.append(Violation(f"{path}.code", Issue.DUPLICATE_INPUT))
    else:
        seen.add(code)
        definition = method.input(code) if method is not None else None
    unit = item.get("unit")
    if unit is None:
        violations.append(Violation(f"{path}.unit", Issue.REQUIRED))
    elif not isinstance(unit, str) or (definition is not None and unit != definition.unit):
        violations.append(Violation(f"{path}.unit", Issue.INVALID_UNIT))
    value = _input_value(item.get("value"), definition, f"{path}.value", violations)
    if definition is None or value is None or len(violations) != before:
        return None
    return MetricInput(code=definition.code, value=Decimal(value), unit=definition.unit)


def _input_value(
    value: object, definition: InputDefinition | None, field: str, violations: list[Violation]
) -> int | None:
    if value is None:
        violations.append(Violation(field, Issue.REQUIRED))
        return None
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or (definition is not None and not definition.minimum <= value <= definition.maximum)
    ):
        # Decisión del 2026-10-14: un valor no entero o no numérico es `out_of_range`.
        violations.append(Violation(field, Issue.OUT_OF_RANGE))
        return None
    return value


def _inputs(
    raw: object, method: MethodDefinition | None, violations: list[Violation]
) -> tuple[MetricInput, ...]:
    if raw is None or not isinstance(raw, list):
        violations.append(Violation("inputs", Issue.REQUIRED))
        return ()
    seen: set[str] = set()
    parsed = {
        item.code: item
        for index, entry in enumerate(raw)
        if (item := _input(index, entry, method, seen, violations)) is not None
    }
    if method is None:
        return ()
    if any(item.required and item.code not in seen for item in method.inputs):
        violations.append(Violation("inputs", Issue.REQUIRED))
    return tuple(parsed[item.code] for item in method.inputs if item.code in parsed)


def validate_calculation(
    body: object,
    catalog: MetricCatalog,
    now: datetime,
    transport_violations: tuple[Violation, ...] = (),
) -> ValidatedCalculation:
    """Solicitud normalizada o `InvalidMetricCalculation` con todas las reglas incumplidas.

    `body` es el JSON recibido; `athlete_id` u otro campo no previsto es
    `field_not_allowed` porque la identidad sale del token.
    """
    violations = list(transport_violations)
    if not isinstance(body, dict):
        violations.append(Violation(None, Issue.REQUIRED))
        raise InvalidMetricCalculation(violations)
    violations.extend(
        Violation(name, Issue.FIELD_NOT_ALLOWED) for name in body if name not in REQUEST_FIELDS
    )
    calculation_id = _calculation_id(body.get("calculation_id"), violations)
    method = _method(body, catalog, violations)
    measured_at = parse_measured_at(body.get("measured_at"), now, violations)
    inputs = _inputs(body.get("inputs"), method, violations)
    if violations or calculation_id is None or method is None or measured_at is None:
        raise InvalidMetricCalculation(violations)
    return ValidatedCalculation(
        data=CalculationData(
            calculation_id=calculation_id,
            metric_code=method.metric_code,
            method_code=method.method_code,
            method_version=method.method_version,
            measured_at=measured_at,
            inputs=inputs,
        ),
        method=method,
    )
