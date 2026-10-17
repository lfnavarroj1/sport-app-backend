"""Sección fisiológica del perfil (HU005, ADR-034).

Son **datos de salud**: exigen el consentimiento vigente, nunca aparecen en logs,
trazas, auditoría ni errores, y el sistema no clasifica ni diagnostica (el IMC es un
cálculo, sin rangos clínicos ni etiquetas).
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from users_management.domain.profile.validation import (
    Collector,
    InvalidProfileSection,
    Issue,
    Violation,
    format_decimal,
    format_instant,
)

WEIGHT_MIN_KG = Decimal("20.0")
WEIGHT_MAX_KG = Decimal("300.0")
HEIGHT_MIN_CM = 100
HEIGHT_MAX_CM = 250
RESTING_HEART_RATE_MIN = 30
RESTING_HEART_RATE_MAX = 220
SYSTOLIC_MIN = 60
SYSTOLIC_MAX = 250
DIASTOLIC_MIN = 30
DIASTOLIC_MAX = 150
TEMPERATURE_MIN_C = Decimal("34.0")
TEMPERATURE_MAX_C = Decimal("42.0")
MEASURED_AT_MAX_FUTURE = timedelta(minutes=5)
MEASURED_AT_MAX_AGE = timedelta(days=365)
# UTC con sufijo `Z` y precisión de segundos.
_MEASURED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_ONE_DECIMAL = Decimal("0.1")
_CENTIMETRES_PER_METRE = Decimal(100)


@dataclass(frozen=True)
class BloodPressureInput:
    systolic_mmhg: int | None = None
    diastolic_mmhg: int | None = None

    def __repr__(self) -> str:
        return "BloodPressureInput()"


@dataclass(frozen=True)
class PhysiologicalInput:
    """Valores con la forma JSON correcta; lo demás ya está en `transport_violations`."""

    expected_version: int | None = None
    consent_version: str | None = None
    measured_at: str | None = None
    weight_kg: str | None = None
    height_cm: int | None = None
    resting_heart_rate_bpm: int | None = None
    blood_pressure: BloodPressureInput | None = None
    body_temperature_c: str | None = None
    transport_violations: tuple[Violation, ...] = ()

    def __repr__(self) -> str:
        # Datos de salud: nunca en trazas ni logs.
        return f"PhysiologicalInput(expected_version={self.expected_version})"


@dataclass(frozen=True)
class BloodPressure:
    systolic_mmhg: int
    diastolic_mmhg: int

    def __repr__(self) -> str:
        return "BloodPressure()"


def body_mass_index(weight_kg: Decimal, height_cm: int) -> Decimal:
    """`peso / altura²` con un decimal y `ROUND_HALF_UP` (68.5 kg y 172 cm → 23.2)."""
    height_m = Decimal(height_cm) / _CENTIMETRES_PER_METRE
    return (weight_kg / (height_m * height_m)).quantize(_ONE_DECIMAL, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class PhysiologicalSection:
    expected_version: int | None
    consent_version: str
    measured_at: datetime
    weight_kg: Decimal
    height_cm: int
    resting_heart_rate_bpm: int | None
    blood_pressure: BloodPressure | None
    body_temperature_c: Decimal | None

    def __repr__(self) -> str:
        return f"PhysiologicalSection(expected_version={self.expected_version})"

    @property
    def bmi(self) -> Decimal:
        return body_mass_index(self.weight_kg, self.height_cm)

    def fields(self, now: datetime) -> dict[str, Any]:
        """El consentimiento se acepta en el instante del comando."""
        return {
            "consent": {"version": self.consent_version, "accepted_at": format_instant(now)},
            **self._measurements(),
        }

    def normalized(self) -> dict[str, Any]:
        return {
            "expected_version": self.expected_version,
            "consent": {"version": self.consent_version},
            **self._measurements(),
        }

    def _measurements(self) -> dict[str, Any]:
        pressure = self.blood_pressure
        return {
            "measured_at": format_instant(self.measured_at),
            "weight_kg": format_decimal(self.weight_kg),
            "height_cm": self.height_cm,
            "bmi": format_decimal(self.bmi),
            "resting_heart_rate_bpm": self.resting_heart_rate_bpm,
            "blood_pressure": (
                {"systolic_mmhg": pressure.systolic_mmhg, "diastolic_mmhg": pressure.diastolic_mmhg}
                if pressure
                else None
            ),
            "body_temperature_c": (
                format_decimal(self.body_temperature_c)
                if self.body_temperature_c is not None
                else None
            ),
        }


def _consent(collector: Collector, value: str | None, current_version: str) -> str | None:
    if not collector.required(value, "consent.version"):
        return None
    if value != current_version:
        collector.add("consent.version", Issue.UNSUPPORTED_CONSENT_VERSION)
        return None
    return value


def _measured_at(collector: Collector, value: str | None, now: datetime) -> datetime | None:
    if value is None:
        collector.required(value, "measured_at")
        return None
    try:
        if not _MEASURED_AT.match(value):
            raise ValueError
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        collector.add("measured_at", Issue.INVALID_DATETIME)
        return None
    if parsed > now + MEASURED_AT_MAX_FUTURE:
        collector.add("measured_at", Issue.TOO_FAR_IN_FUTURE)
        return None
    if parsed < now - MEASURED_AT_MAX_AGE:
        collector.add("measured_at", Issue.TOO_OLD)
        return None
    return parsed


def _blood_pressure(collector: Collector, value: BloodPressureInput | None) -> BloodPressure | None:
    if value is None:
        return None
    systolic = diastolic = None
    if collector.required(value.systolic_mmhg, "blood_pressure.systolic_mmhg"):
        systolic = collector.integer(
            value.systolic_mmhg, "blood_pressure.systolic_mmhg", SYSTOLIC_MIN, SYSTOLIC_MAX
        )
    if collector.required(value.diastolic_mmhg, "blood_pressure.diastolic_mmhg"):
        diastolic = collector.integer(
            value.diastolic_mmhg, "blood_pressure.diastolic_mmhg", DIASTOLIC_MIN, DIASTOLIC_MAX
        )
    if systolic is None or diastolic is None:
        return None
    if systolic <= diastolic:
        collector.add("blood_pressure", Issue.INCONSISTENT_VALUES)
        return None
    return BloodPressure(systolic_mmhg=systolic, diastolic_mmhg=diastolic)


def validate_physiological(
    section: PhysiologicalInput, now: datetime, current_consent_version: str
) -> PhysiologicalSection:
    """Valida todo o nada; sin consentimiento vigente no se acepta ningún valor."""
    collector = Collector(section.transport_violations)
    expected_version = collector.expected_version(section.expected_version)
    consent_version = _consent(collector, section.consent_version, current_consent_version)
    measured_at = _measured_at(collector, section.measured_at, now)
    weight_kg = height_cm = None
    if collector.required(section.weight_kg, "weight_kg"):
        weight_kg = collector.decimal(section.weight_kg, "weight_kg", WEIGHT_MIN_KG, WEIGHT_MAX_KG)
    if collector.required(section.height_cm, "height_cm"):
        height_cm = collector.integer(section.height_cm, "height_cm", HEIGHT_MIN_CM, HEIGHT_MAX_CM)
    resting_heart_rate = collector.integer(
        section.resting_heart_rate_bpm,
        "resting_heart_rate_bpm",
        RESTING_HEART_RATE_MIN,
        RESTING_HEART_RATE_MAX,
    )
    blood_pressure = _blood_pressure(collector, section.blood_pressure)
    temperature = collector.decimal(
        section.body_temperature_c, "body_temperature_c", TEMPERATURE_MIN_C, TEMPERATURE_MAX_C
    )
    collector.raise_if_any()
    if consent_version is None or measured_at is None or weight_kg is None or height_cm is None:
        raise InvalidProfileSection([])  # Inalcanzable: la falta ya se informó arriba.
    return PhysiologicalSection(
        expected_version=expected_version,
        consent_version=consent_version,
        measured_at=measured_at,
        weight_kg=weight_kg,
        height_cm=height_cm,
        resting_heart_rate_bpm=resting_heart_rate,
        blood_pressure=blood_pressure,
        body_temperature_c=temperature,
    )
