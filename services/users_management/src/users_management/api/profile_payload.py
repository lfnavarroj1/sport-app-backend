"""Lectura del cuerpo JSON de los `PUT` del perfil con los errores de ADR-034.

Esta capa solo verifica la forma del JSON: campos permitidos (`field_not_allowed`,
incluidos `user_id` y `bmi`) y tipos JSON (`unknown_value`; un número donde va una
cadena decimal también lo es). Los obligatorios, catálogos, rangos, decimales, fechas,
consentimiento y la consistencia de la presión son reglas del dominio.
"""

from typing import Any

from users_management.domain.profile.demographic import DemographicInput
from users_management.domain.profile.physiological import BloodPressureInput, PhysiologicalInput
from users_management.domain.profile.validation import Issue, Violation

DEMOGRAPHIC_FIELDS = (
    "expected_version",
    "birth_date",
    "gender",
    "country_code",
    "city",
    "marital_status",
    "occupation",
    "income_range",
)
PHYSIOLOGICAL_FIELDS = (
    "expected_version",
    "consent",
    "measured_at",
    "weight_kg",
    "height_cm",
    "resting_heart_rate_bpm",
    "blood_pressure",
    "body_temperature_c",
)
CONSENT_FIELDS = ("version",)
BLOOD_PRESSURE_FIELDS = ("systolic_mmhg", "diastolic_mmhg")


def _unexpected(document: dict[str, Any], allowed: tuple[str, ...], prefix: str) -> list[Violation]:
    return [
        Violation(f"{prefix}{name}", Issue.FIELD_NOT_ALLOWED)
        for name in document
        if name not in allowed
    ]


def _string(value: object, path: str, violations: list[Violation]) -> str | None:
    """`None` si falta (el dominio decide si es obligatorio) o si el tipo no es cadena."""
    if value is None:
        return None
    if not isinstance(value, str):
        violations.append(Violation(path, Issue.UNKNOWN_VALUE))
        return None
    return value


def _integer(value: object, path: str, violations: list[Violation]) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        violations.append(Violation(path, Issue.UNKNOWN_VALUE))
        return None
    return value


def _object(value: object, path: str, violations: list[Violation]) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        violations.append(Violation(path, Issue.UNKNOWN_VALUE))
        return None
    return value


def _body_violation(body: object) -> Violation:
    return Violation(None, Issue.REQUIRED if body is None else Issue.UNKNOWN_VALUE)


def parse_demographic(body: object) -> DemographicInput:
    """Sección con los campos de forma válida y las violaciones de transporte."""
    if not isinstance(body, dict):
        return DemographicInput(transport_violations=(_body_violation(body),))
    violations = _unexpected(body, DEMOGRAPHIC_FIELDS, "")
    return DemographicInput(
        expected_version=_integer(body.get("expected_version"), "expected_version", violations),
        birth_date=_string(body.get("birth_date"), "birth_date", violations),
        gender=_string(body.get("gender"), "gender", violations),
        country_code=_string(body.get("country_code"), "country_code", violations),
        city=_string(body.get("city"), "city", violations),
        marital_status=_string(body.get("marital_status"), "marital_status", violations),
        occupation=_string(body.get("occupation"), "occupation", violations),
        income_range=_string(body.get("income_range"), "income_range", violations),
        transport_violations=tuple(violations),
    )


def _consent_version(body: dict[str, Any], violations: list[Violation]) -> str | None:
    consent = _object(body.get("consent"), "consent", violations)
    if consent is None:
        return None
    violations.extend(_unexpected(consent, CONSENT_FIELDS, "consent."))
    return _string(consent.get("version"), "consent.version", violations)


def _blood_pressure(body: dict[str, Any], violations: list[Violation]) -> BloodPressureInput | None:
    pressure = _object(body.get("blood_pressure"), "blood_pressure", violations)
    if pressure is None:
        return None
    violations.extend(_unexpected(pressure, BLOOD_PRESSURE_FIELDS, "blood_pressure."))
    return BloodPressureInput(
        systolic_mmhg=_integer(
            pressure.get("systolic_mmhg"), "blood_pressure.systolic_mmhg", violations
        ),
        diastolic_mmhg=_integer(
            pressure.get("diastolic_mmhg"), "blood_pressure.diastolic_mmhg", violations
        ),
    )


def parse_physiological(body: object) -> PhysiologicalInput:
    """Sección con los campos de forma válida y las violaciones de transporte."""
    if not isinstance(body, dict):
        return PhysiologicalInput(transport_violations=(_body_violation(body),))
    violations = _unexpected(body, PHYSIOLOGICAL_FIELDS, "")
    return PhysiologicalInput(
        expected_version=_integer(body.get("expected_version"), "expected_version", violations),
        consent_version=_consent_version(body, violations),
        measured_at=_string(body.get("measured_at"), "measured_at", violations),
        # Los decimales viajan como cadena (ADR-027): un número JSON es `unknown_value`.
        weight_kg=_string(body.get("weight_kg"), "weight_kg", violations),
        height_cm=_integer(body.get("height_cm"), "height_cm", violations),
        resting_heart_rate_bpm=_integer(
            body.get("resting_heart_rate_bpm"), "resting_heart_rate_bpm", violations
        ),
        blood_pressure=_blood_pressure(body, violations),
        body_temperature_c=_string(
            body.get("body_temperature_c"), "body_temperature_c", violations
        ),
        transport_violations=tuple(violations),
    )
