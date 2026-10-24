"""Lotes de mediciones de HU037 (ADR-044 §5-§7).

- El móvil envía cada 5 s un lote de hasta 60 muestras de 1 Hz de un dispositivo vinculado.
- Cada muestra se valida por separado (CA3): `type = HEART_RATE`, `unit = bpm`, `value` entero
  entre 20 y 250, `measured_at` UTC con segundos entre el inicio de la sesión y ahora + 5 s, y
  con antigüedad de 15 minutos como máximo. Las inválidas se devuelven en `rejected[]` con
  su posición y un motivo; el resto del lote continúa.
- Idempotencia natural (CA4): la identidad de una muestra es (`session_id`, `device_id`,
  `measured_at`); una repetida, del mismo lote o de uno anterior, se cuenta en `duplicates`.
  Las muestras fuera de orden se aceptan.

La evaluación de riesgo, el evento `medicion.recibida.v1` y la retención pertenecen a HU036.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from training_management.domain.devices.model import device_id_problem
from training_management.domain.sessions.validation import Issue

BATCH_FIELDS = ("device_id", "samples")
TOO_MANY_ITEMS = "too_many_items"
MAX_SAMPLES = 60
MIN_HEART_RATE = 20
MAX_HEART_RATE = 250
FUTURE_TOLERANCE = timedelta(seconds=5)
MAX_SAMPLE_AGE = timedelta(minutes=15)
SAMPLE_FIELDS = ("measured_at", "type", "value", "unit")
_UTC_SECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class MeasurementType(StrEnum):
    HEART_RATE = "HEART_RATE"


class MeasurementUnit(StrEnum):
    BPM = "bpm"


class SampleIssue(StrEnum):
    """Motivos por muestra (`rejected[].issue`)."""

    REQUIRED = Issue.REQUIRED.value
    FIELD_NOT_ALLOWED = Issue.FIELD_NOT_ALLOWED.value
    UNKNOWN_VALUE = Issue.UNKNOWN_VALUE.value
    INVALID_UNIT = "invalid_unit"
    OUT_OF_RANGE = "out_of_range"
    INVALID_DATETIME = Issue.INVALID_DATETIME.value
    TOO_FAR_IN_FUTURE = Issue.TOO_FAR_IN_FUTURE.value
    TOO_OLD = Issue.TOO_OLD.value


class InvalidMeasurementBatch(Exception):
    """El lote no tiene la forma de ADR-044 (no se evalúa ninguna muestra)."""

    code = "validation_error"

    def __init__(self, violations: list[tuple[str | None, str]]) -> None:
        super().__init__(", ".join(f"{field}: {issue}" for field, issue in violations))
        self.violations = tuple(violations)


@dataclass(frozen=True)
class MeasurementBatch:
    """Lote con forma válida; las muestras se validan después, con la sesión."""

    session_id: UUID
    device_id: str
    samples: tuple[Any, ...]


def validate_batch(raw_session_id: str, body: object) -> MeasurementBatch:
    """Ruta y cuerpo del lote, o `InvalidMeasurementBatch` con todas las reglas incumplidas."""
    violations: list[tuple[str | None, str]] = []
    session_id = None
    try:
        session_id = UUID(raw_session_id)
    except ValueError:
        violations.append(("session_id", Issue.INVALID_UUID.value))
    if not isinstance(body, dict):
        issue = Issue.REQUIRED if body is None else Issue.UNKNOWN_VALUE
        raise InvalidMeasurementBatch([*violations, (None, issue.value)])
    violations.extend(
        (name, Issue.FIELD_NOT_ALLOWED.value) for name in body if name not in BATCH_FIELDS
    )
    device_id = body.get("device_id")
    if device_id is None:
        violations.append(("device_id", Issue.REQUIRED.value))
    elif not isinstance(device_id, str):
        violations.append(("device_id", Issue.UNKNOWN_VALUE.value))
    elif (problem := device_id_problem(device_id)) is not None:
        violations.append(("device_id", problem))
    samples = body.get("samples")
    if samples is None or samples == []:
        violations.append(("samples", Issue.REQUIRED.value))
    elif not isinstance(samples, list):
        violations.append(("samples", Issue.UNKNOWN_VALUE.value))
    elif len(samples) > MAX_SAMPLES:
        violations.append(("samples", TOO_MANY_ITEMS))
    if (
        violations
        or session_id is None
        or not isinstance(device_id, str)
        or not isinstance(samples, list)
    ):
        raise InvalidMeasurementBatch(violations)
    return MeasurementBatch(session_id=session_id, device_id=device_id, samples=tuple(samples))


@dataclass(frozen=True)
class Measurement:
    measured_at: datetime
    type: MeasurementType
    value: int
    unit: MeasurementUnit


@dataclass(frozen=True)
class RejectedSample:
    index: int
    issue: SampleIssue


@dataclass(frozen=True)
class SampleCheck:
    """Muestras válidas (con su posición) y rechazadas de un lote."""

    valid: tuple[tuple[int, Measurement], ...]
    rejected: tuple[RejectedSample, ...]


def _shape_issue(sample: object) -> SampleIssue | None:
    if not isinstance(sample, dict):
        return SampleIssue.UNKNOWN_VALUE
    if any(name not in SAMPLE_FIELDS for name in sample):
        return SampleIssue.FIELD_NOT_ALLOWED
    if any(sample.get(name) is None for name in SAMPLE_FIELDS):
        return SampleIssue.REQUIRED
    value = sample["value"]
    if (
        not isinstance(sample["measured_at"], str)
        or not isinstance(sample["type"], str)
        or not isinstance(sample["unit"], str)
        or isinstance(value, bool)
        or not isinstance(value, int)
    ):
        return SampleIssue.UNKNOWN_VALUE
    return None


def _instant(
    raw: str, started_at: datetime, now: datetime
) -> tuple[datetime | None, SampleIssue | None]:
    if not _UTC_SECONDS.match(raw):
        return None, SampleIssue.INVALID_DATETIME
    try:
        instant = datetime.fromisoformat(raw).astimezone(UTC)
    except ValueError:
        return None, SampleIssue.INVALID_DATETIME
    if instant > now + FUTURE_TOLERANCE:
        return None, SampleIssue.TOO_FAR_IN_FUTURE
    if instant < now - MAX_SAMPLE_AGE:
        return None, SampleIssue.TOO_OLD
    if instant < started_at:
        return None, SampleIssue.OUT_OF_RANGE
    return instant, None


def check_sample(sample: Any, *, started_at: datetime, now: datetime) -> Measurement | SampleIssue:
    """La muestra normalizada o el primer motivo de rechazo: forma, tipo, unidad, valor e
    instante, en ese orden."""
    problem = _shape_issue(sample)
    if problem is not None:
        return problem
    if sample["type"] != MeasurementType.HEART_RATE.value:
        return SampleIssue.UNKNOWN_VALUE
    if sample["unit"] != MeasurementUnit.BPM.value:
        return SampleIssue.INVALID_UNIT
    value: int = sample["value"]
    if not MIN_HEART_RATE <= value <= MAX_HEART_RATE:
        return SampleIssue.OUT_OF_RANGE
    instant, issue = _instant(sample["measured_at"], started_at, now.astimezone(UTC))
    if instant is None:
        return issue or SampleIssue.INVALID_DATETIME
    return Measurement(
        measured_at=instant,
        type=MeasurementType.HEART_RATE,
        value=value,
        unit=MeasurementUnit.BPM,
    )


def check_samples(samples: Sequence[Any], *, started_at: datetime, now: datetime) -> SampleCheck:
    valid: list[tuple[int, Measurement]] = []
    rejected: list[RejectedSample] = []
    for index, sample in enumerate(samples):
        result = check_sample(sample, started_at=started_at, now=now)
        if isinstance(result, Measurement):
            valid.append((index, result))
        else:
            rejected.append(RejectedSample(index=index, issue=result))
    return SampleCheck(valid=tuple(valid), rejected=tuple(rejected))


def first_occurrences(valid: tuple[tuple[int, Measurement], ...]) -> tuple[Measurement, ...]:
    """Las muestras válidas sin repetir `measured_at` dentro del lote (gana la primera)."""
    seen: set[datetime] = set()
    unique: list[Measurement] = []
    for _, measurement in valid:
        if measurement.measured_at not in seen:
            seen.add(measurement.measured_at)
            unique.append(measurement)
    return tuple(unique)
