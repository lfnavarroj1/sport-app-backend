"""Lectura del cuerpo JSON de los `PUT` del perfil con los errores de ADR-034 (HU005 y HU006).

Esta capa solo verifica la forma del JSON: campos permitidos (`field_not_allowed`,
incluidos `user_id` y `bmi`) y tipos JSON (`unknown_value`; un número donde va una
cadena decimal también lo es). Los obligatorios, catálogos, rangos, decimales, fechas,
consentimiento y la consistencia de la presión son reglas del dominio.
"""

from typing import Any

from users_management.domain.profile.demographic import DemographicInput
from users_management.domain.profile.physiological import BloodPressureInput, PhysiologicalInput
from users_management.domain.profile.sport_habits import SportHabitsInput, SportInput
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
SPORT_HABITS_FIELDS = ("expected_version", "sports", "availability", "primary_goal")
SPORT_FIELDS = (
    "sport",
    "frequency_per_week",
    "session_duration_minutes",
    "intensity",
    "level",
    "years_practicing",
)
AVAILABILITY_FIELDS = ("days", "time_slots")
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


def _array(value: object, path: str, violations: list[Violation]) -> list[Any] | None:
    if value is None:
        return None
    if not isinstance(value, list):
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


def _sport(item: object, path: str, violations: list[Violation]) -> SportInput | None:
    sport = _object(item, path, violations)
    if sport is None:
        if item is None:
            violations.append(Violation(path, Issue.UNKNOWN_VALUE))
        return None
    violations.extend(_unexpected(sport, SPORT_FIELDS, f"{path}."))
    return SportInput(
        sport=_string(sport.get("sport"), f"{path}.sport", violations),
        frequency_per_week=_integer(
            sport.get("frequency_per_week"), f"{path}.frequency_per_week", violations
        ),
        session_duration_minutes=_integer(
            sport.get("session_duration_minutes"), f"{path}.session_duration_minutes", violations
        ),
        intensity=_string(sport.get("intensity"), f"{path}.intensity", violations),
        level=_string(sport.get("level"), f"{path}.level", violations),
        years_practicing=_integer(
            sport.get("years_practicing"), f"{path}.years_practicing", violations
        ),
    )


def _strings(
    value: object, path: str, violations: list[Violation]
) -> tuple[str | None, ...] | None:
    """Lista de cadenas; un elemento de otro tipo se informa por su posición."""
    items = _array(value, path, violations)
    if items is None:
        return None
    values: list[str | None] = []
    for position, item in enumerate(items):
        if isinstance(item, str):
            values.append(item)
        else:
            violations.append(Violation(f"{path}[{position}]", Issue.UNKNOWN_VALUE))
            values.append(None)
    return tuple(values)


def parse_sport_habits(body: object) -> SportHabitsInput:
    """HU006: sección con los campos de forma válida y las violaciones de transporte."""
    if not isinstance(body, dict):
        return SportHabitsInput(transport_violations=(_body_violation(body),))
    violations = _unexpected(body, SPORT_HABITS_FIELDS, "")
    sports = _array(body.get("sports"), "sports", violations)
    availability = _object(body.get("availability"), "availability", violations)
    days = time_slots = None
    if availability is not None:
        violations.extend(_unexpected(availability, AVAILABILITY_FIELDS, "availability."))
        days = _strings(availability.get("days"), "availability.days", violations)
        time_slots = _strings(availability.get("time_slots"), "availability.time_slots", violations)
    return SportHabitsInput(
        expected_version=_integer(body.get("expected_version"), "expected_version", violations),
        sports=(
            tuple(_sport(item, f"sports[{i}]", violations) for i, item in enumerate(sports))
            if sports is not None
            else None
        ),
        has_availability=availability is not None,
        days=days,
        time_slots=time_slots,
        primary_goal=_string(body.get("primary_goal"), "primary_goal", violations),
        transport_violations=tuple(violations),
    )
