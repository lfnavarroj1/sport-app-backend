"""Umbral y regla de riesgo de HU036 (ADR-044 §8-§9).

Umbral (`max_heart_rate_alert_bpm`):
- configurable por el deportista entre 100 y 220 lpm (`PUT /v1/me/monitoring-settings`);
- sin configuración, `round(0.9 x (220 - edad))` (redondeo `ROUND_HALF_UP`) con la edad que
  resulta de `birth_date` (contexto interno de Usuarios, ADR-041) o `171` sin fecha. El valor
  calculado se acota al mismo rango 100-220 que el configurable.

Regla única `HR_ABOVE_THRESHOLD_V1`, evaluada por dispositivo con las muestras del lote y las
de los 30 s previos:
- las muestras son de 1 Hz con precisión de segundos (HU037): dos muestras son consecutivas si
  distan exactamente 1 s; un hueco corta la racha;
- hay riesgo en el instante `t` si todas las muestras consecutivas que terminan en `t` superan
  el umbral (`value > umbral`) durante al menos 15 s (15 muestras de 1 s: `t - inicio + 1 s`);
- se genera una alerta en cada instante nuevo del lote con riesgo, salvo que exista otra alerta
  de la sesión a menos de 60 s (la alerta repite como máximo una vez cada 60 s);
- `NO_DATA` si la muestra más reciente del dispositivo en la sesión tiene más de 30 s respecto
  del reloj del backend: es pérdida de monitoreo, no se evalúa ni se alerta (CA3);
- `RISK` si el lote generó una alerta o la muestra más reciente está en riesgo; si no, `OK`.

La evaluación es determinista para las mismas muestras, el mismo umbral, las mismas alertas
previas y el mismo reloj. No diagnostica: el texto aprobado lo declara (R-21).
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from training_management.domain.sessions.validation import Issue

RULE = "HR_ABOVE_THRESHOLD_V1"
THRESHOLD_MIN = 100
THRESHOLD_MAX = 220
DEFAULT_THRESHOLD = 171
MAX_HEART_RATE_BASE = 220
DEFAULT_FACTOR = Decimal("0.9")
RISK_DURATION = timedelta(seconds=15)
SAMPLE_INTERVAL = timedelta(seconds=1)
ALERT_REPEAT = timedelta(seconds=60)
NO_DATA_AFTER = timedelta(seconds=30)
LOOKBACK = timedelta(seconds=30)
SETTINGS_FIELDS = ("max_heart_rate_alert_bpm",)
ALERT_MESSAGE = (
    "Tu frecuencia cardiaca supera el límite que configuraste. Reduce la intensidad o detén "
    "la práctica. Esto no es un diagnóstico médico."
)
ALERT_INSTRUCTION = "Reduce la intensidad o detén la práctica."
DISCLAIMER = "Esto no es un diagnóstico médico."
# Espacio de nombres propio para que el `alert_id` sea estable ante reintentos.
_ALERT_NAMESPACE = uuid.UUID("5f1f3c2e-0d4b-5a8e-9c36-7b2a4e0d9c41")


class EvaluationStatus(StrEnum):
    OK = "OK"
    RISK = "RISK"
    NO_DATA = "NO_DATA"


class AlertSeverity(StrEnum):
    HIGH = "HIGH"


class ThresholdSource(StrEnum):
    """De dónde sale el umbral vigente."""

    CUSTOM = "CUSTOM"
    AGE_DEFAULT = "AGE_DEFAULT"
    FIXED_DEFAULT = "FIXED_DEFAULT"


class InvalidMonitoringSettings(Exception):
    """El cuerpo de `PUT /v1/me/monitoring-settings` incumple ADR-044 §9."""

    code = "validation_error"

    def __init__(self, violations: list[tuple[str | None, str]]) -> None:
        super().__init__(", ".join(f"{field}: {issue}" for field, issue in violations))
        self.violations = tuple(violations)


@dataclass(frozen=True)
class MonitoringSettings:
    athlete_id: UUID
    max_heart_rate_alert_bpm: int
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(cls, athlete_id: UUID, threshold: int, now: datetime) -> "MonitoringSettings":
        instant = now.astimezone(UTC)
        return cls(athlete_id, threshold, 1, instant, instant)

    def change(self, threshold: int, now: datetime) -> "MonitoringSettings":
        """Mismo umbral: sin cambios (PUT idempotente); otro: versión siguiente."""
        if threshold == self.max_heart_rate_alert_bpm:
            return self
        return MonitoringSettings(
            athlete_id=self.athlete_id,
            max_heart_rate_alert_bpm=threshold,
            version=self.version + 1,
            created_at=self.created_at,
            updated_at=now.astimezone(UTC),
        )


@dataclass(frozen=True)
class Threshold:
    """Umbral vigente; `settings` es `None` cuando se usa un valor por defecto."""

    value: int
    source: ThresholdSource
    settings: MonitoringSettings | None = None


def validate_settings(body: object) -> int:
    """El umbral del cuerpo o `InvalidMonitoringSettings` con todas las reglas incumplidas."""
    if not isinstance(body, dict):
        issue = Issue.REQUIRED if body is None else Issue.UNKNOWN_VALUE
        raise InvalidMonitoringSettings([(None, issue.value)])
    violations: list[tuple[str | None, str]] = [
        (name, Issue.FIELD_NOT_ALLOWED.value) for name in body if name not in SETTINGS_FIELDS
    ]
    value: Any = body.get("max_heart_rate_alert_bpm")
    if value is None:
        violations.append(("max_heart_rate_alert_bpm", Issue.REQUIRED.value))
    elif isinstance(value, bool) or not isinstance(value, int):
        violations.append(("max_heart_rate_alert_bpm", Issue.UNKNOWN_VALUE.value))
    elif not THRESHOLD_MIN <= value <= THRESHOLD_MAX:
        violations.append(("max_heart_rate_alert_bpm", "out_of_range"))
    if violations:
        raise InvalidMonitoringSettings(violations)
    threshold: int = value
    return threshold


def age_on(birth_date: date, today: date) -> int:
    """Años cumplidos en `today`."""
    before_birthday = (today.month, today.day) < (birth_date.month, birth_date.day)
    return today.year - birth_date.year - int(before_birthday)


def default_threshold(birth_date: date | None, today: date) -> Threshold:
    if birth_date is None:
        return Threshold(DEFAULT_THRESHOLD, ThresholdSource.FIXED_DEFAULT)
    raw = DEFAULT_FACTOR * (MAX_HEART_RATE_BASE - age_on(birth_date, today))
    value = int(raw.quantize(Decimal(1), ROUND_HALF_UP))
    return Threshold(min(max(value, THRESHOLD_MIN), THRESHOLD_MAX), ThresholdSource.AGE_DEFAULT)


@dataclass(frozen=True)
class RiskAlert:
    alert_id: UUID
    session_id: UUID
    device_id: str
    rule: str
    severity: AlertSeverity
    detected_at: datetime
    threshold_bpm: int


@dataclass(frozen=True)
class Evaluation:
    status: EvaluationStatus
    alerts: tuple[RiskAlert, ...]


def alert_id(session_id: UUID, device_id: str, detected_at: datetime) -> UUID:
    """UUIDv5 de la sesión, el dispositivo, el instante y la regla."""
    instant = detected_at.astimezone(UTC).isoformat()
    return uuid.uuid5(_ALERT_NAMESPACE, f"{session_id}:{device_id}:{instant}:{RULE}")


def window_start(new_instants: Iterable[datetime], latest: datetime) -> datetime:
    """Desde cuándo se leen muestras: 30 s antes de la más antigua del lote (o de la última)."""
    return min([*new_instants, latest]) - LOOKBACK


def _risk_instants(samples: Sequence[tuple[datetime, int]], threshold: int) -> set[datetime]:
    """Instantes en los que la racha de muestras consecutivas sobre el umbral dura ≥ 15 s."""
    risky: set[datetime] = set()
    run_start: datetime | None = None
    previous: datetime | None = None
    for instant, value in sorted(samples):
        if value > threshold:
            consecutive = previous is not None and instant - previous == SAMPLE_INTERVAL
            if run_start is None or not consecutive:
                run_start = instant
            if instant - run_start + SAMPLE_INTERVAL >= RISK_DURATION:
                risky.add(instant)
        else:
            run_start = None
        previous = instant
    return risky


def evaluate(
    *,
    session_id: UUID,
    device_id: str,
    samples: Sequence[tuple[datetime, int]],
    new_instants: Iterable[datetime],
    threshold: int,
    previous_alerts: Iterable[datetime],
    now: datetime,
) -> Evaluation:
    """Evalúa el lote con `samples` (la ventana del dispositivo ya con las muestras nuevas).

    `previous_alerts` son los `detected_at` de las alertas ya registradas en la sesión cerca
    de la ventana; las nuevas alertas no quedan a menos de 60 s de ninguna de ellas.
    """
    if not samples:
        return Evaluation(EvaluationStatus.NO_DATA, ())
    latest = max(instant for instant, _ in samples)
    if now.astimezone(UTC) - latest > NO_DATA_AFTER:
        return Evaluation(EvaluationStatus.NO_DATA, ())
    risky = _risk_instants(samples, threshold)
    alerted = list(previous_alerts)
    alerts: list[RiskAlert] = []
    for instant in sorted(set(new_instants) & risky):
        if all(abs(instant - other) >= ALERT_REPEAT for other in alerted):
            alerted.append(instant)
            alerts.append(
                RiskAlert(
                    alert_id=alert_id(session_id, device_id, instant),
                    session_id=session_id,
                    device_id=device_id,
                    rule=RULE,
                    severity=AlertSeverity.HIGH,
                    detected_at=instant,
                    threshold_bpm=threshold,
                )
            )
    at_risk = bool(alerts) or latest in risky
    return Evaluation(EvaluationStatus.RISK if at_risk else EvaluationStatus.OK, tuple(alerts))


@dataclass(frozen=True)
class BatchSummary:
    """Resumen del lote para `medicion.recibida.v1` (ADR-044 §7); `None` sin muestras nuevas."""

    session_id: UUID
    device_id: str
    window_from: datetime
    window_to: datetime
    samples_accepted: int
    last_value: int
    max_value: int
    avg_value: Decimal
    version: int


AVERAGE_QUANTUM = Decimal("0.1")


def summarize(
    session_id: UUID, device_id: str, accepted: Sequence[tuple[datetime, int]], version: int
) -> BatchSummary:
    """Ventana, conteo, último valor (el de `window_to`), máximo y promedio con un decimal."""
    if not accepted:
        raise ValueError("Un resumen necesita al menos una muestra aceptada.")
    ordered = sorted(accepted)
    values = [value for _, value in ordered]
    average = (Decimal(sum(values)) / len(values)).quantize(AVERAGE_QUANTUM, ROUND_HALF_UP)
    return BatchSummary(
        session_id=session_id,
        device_id=device_id,
        window_from=ordered[0][0],
        window_to=ordered[-1][0],
        samples_accepted=len(values),
        last_value=values[-1],
        max_value=max(values),
        avg_value=average,
        version=version,
    )
