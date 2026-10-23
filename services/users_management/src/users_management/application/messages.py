"""Lectura validada de los eventos de `sportapp.usuarios.v1` para `usuarios-read-projector`.

Se proyectan `usuario.registrado.v1` (HU001, directorio de identidad),
`perfil.demografico_actualizado.v1` / `perfil.fisiologico_actualizado.v1` (HU005) y
`habitos.deportivos_actualizados.v1` (HU006), los dos últimos según ADR-034. Cada evento
se valida campo a campo contra su esquema versionado en `contracts/events/usuarios/` y el
payload no admite campos fuera del contrato (minimización): un mensaje que no lo cumple es
un `InvalidEvent`, no se confirma y, agotadas las entregas, termina en la DLQ. Otro tipo de
evento del tópico es `UnsupportedEvent`: este proyector todavía no lo proyecta y se confirma
sin efectos.

Los motivos de rechazo nombran el campo, nunca su valor: el payload contiene datos
demográficos y de salud.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from users_management.domain.account.events import UserRegistered
from users_management.domain.account.model import AccountStatus, ActorType
from users_management.domain.profile.events import (
    SCHEMA_VERSION as PROFILE_SCHEMA_VERSION,
)
from users_management.domain.profile.events import (
    SECTION_BY_EVENT_TYPE,
    ProfileSection,
)
from users_management.domain.profile.sport_habits import (
    DAYS_MAX,
    DURATION_MAX,
    DURATION_MIN,
    FREQUENCY_MAX,
    FREQUENCY_MIN,
    SPORTS_MAX,
    TIME_SLOTS_MAX,
    YEARS_MAX,
    YEARS_MIN,
    Day,
    Intensity,
    Level,
    PrimaryGoal,
    Sport,
    TimeSlot,
)

PRODUCER = "gestion-usuarios"
# `usuario.registrado.v1` crea la cuenta: su versión es siempre 1 (esquema: `const`).
REGISTERED_VERSION = 1

# Catálogos copiados de los esquemas versionados; la prueba de contrato verifica que
# coincidan con ellos (`tests/contract/test_profile_event_consumption.py`).
GENDERS = frozenset({"FEMALE", "MALE", "NON_BINARY", "PREFER_NOT_TO_SAY"})
MARITAL_STATUSES = frozenset(
    {"SINGLE", "MARRIED", "DOMESTIC_PARTNERSHIP", "DIVORCED", "WIDOWED", "PREFER_NOT_TO_SAY"}
)
INCOME_RANGES = frozenset(
    {"LT_1_SMMLV", "1_2_SMMLV", "2_4_SMMLV", "GT_4_SMMLV", "PREFER_NOT_TO_SAY"}
)

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UTC_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
_UTC_SECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_ONE_DECIMAL = re.compile(r"^\d{1,3}\.\d$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")

_REGISTERED_FIELDS = frozenset(
    {"user_id", "full_name", "email", "actor_type", "status", "registered_at"}
)
DEMOGRAPHIC_FIELDS = frozenset(
    {
        "user_id",
        "version",
        "birth_date",
        "gender",
        "country_code",
        "city",
        "marital_status",
        "occupation",
        "income_range",
        "created_at",
        "updated_at",
    }
)
PHYSIOLOGICAL_FIELDS = frozenset(
    {
        "user_id",
        "version",
        "consent",
        "measured_at",
        "weight_kg",
        "height_cm",
        "bmi",
        "resting_heart_rate_bpm",
        "blood_pressure",
        "body_temperature_c",
        "created_at",
        "updated_at",
    }
)
SPORT_HABITS_FIELDS = frozenset(
    {"user_id", "version", "sports", "availability", "primary_goal", "created_at", "updated_at"}
)
_SPORT_FIELDS = frozenset(
    {
        "sport",
        "frequency_per_week",
        "session_duration_minutes",
        "intensity",
        "level",
        "years_practicing",
    }
)
_AVAILABILITY_FIELDS = frozenset({"days", "time_slots"})
_CONSENT_FIELDS = frozenset({"version", "accepted_at"})
_BLOOD_PRESSURE_FIELDS = frozenset({"systolic_mmhg", "diastolic_mmhg"})


class InvalidEvent(Exception):
    """El mensaje no cumple el contrato; el motivo nombra el campo, nunca su valor."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnsupportedEvent(Exception):
    """Evento válido del tópico que este proyector no proyecta."""

    def __init__(self, event_type: str) -> None:
        super().__init__(event_type)
        self.event_type = event_type


@dataclass(frozen=True)
class UserRegisteredEvent:
    """`usuario.registrado.v1` reducido a lo que proyecta `user_directory`.

    Desde HU011 (CA5) también conserva `full_name`: lo necesita la lista de deportistas
    del profesional (`POST /v1/users/lookup-batch`).
    """

    event_id: UUID
    user_id: UUID
    aggregate_version: int
    correlation_id: UUID | None
    full_name: str
    email: str
    actor_type: str
    status: str

    @property
    def aggregate_id(self) -> UUID:
        return self.user_id

    def __repr__(self) -> str:
        # El correo es un dato personal: nunca en trazas.
        return f"UserRegisteredEvent(event_id={self.event_id}, user_id={self.user_id})"


@dataclass(frozen=True)
class ProfileSectionUpdatedEvent:
    """`perfil.*_actualizado.v1`: `document` es el payload sin `user_id` (ADR-034)."""

    event_id: UUID
    event_type: str
    section: ProfileSection
    user_id: UUID
    aggregate_version: int
    correlation_id: UUID | None
    document: dict[str, Any]

    @property
    def aggregate_id(self) -> UUID:
        return self.user_id

    def __repr__(self) -> str:
        # Datos demográficos y de salud: nunca en trazas.
        return (
            f"ProfileSectionUpdatedEvent(event_id={self.event_id}, section={self.section}, "
            f"version={self.aggregate_version})"
        )


UsersEvent = UserRegisteredEvent | ProfileSectionUpdatedEvent


def _uuid(value: object, field: str) -> UUID:
    if not isinstance(value, str):
        raise InvalidEvent(f"{field}_not_uuid")
    try:
        return UUID(value)
    except ValueError:
        raise InvalidEvent(f"{field}_not_uuid") from None


def _int(value: object, field: str, minimum: int, maximum: int | None = None) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise InvalidEvent(f"{field}_out_of_range")
    return value


def _text(value: object, field: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise InvalidEvent(f"{field}_invalid")
    return value


def _optional(value: object, check: Callable[[object], object]) -> None:
    if value is not None:
        check(value)


def _choice(value: object, field: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise InvalidEvent(f"unknown_{field}")
    return value


def _matches(value: object, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise InvalidEvent(f"{field}_invalid")
    return value


def _instant(value: object, field: str, pattern: re.Pattern[str] = _UTC_INSTANT) -> None:
    """Instante en UTC con sufijo `Z` (regla del repositorio: el frontend convierte)."""
    text = _matches(value, field, pattern)
    try:
        datetime.fromisoformat(text)
    except ValueError:
        raise InvalidEvent(f"{field}_invalid") from None


def _aware_datetime(value: object, field: str) -> None:
    if not isinstance(value, str):
        raise InvalidEvent(f"{field}_not_datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise InvalidEvent(f"{field}_not_datetime") from None
    if parsed.tzinfo is None:
        raise InvalidEvent(f"{field}_without_timezone")


def _date(value: object, field: str) -> None:
    text = _matches(value, field, _DATE)
    try:
        date.fromisoformat(text)
    except ValueError:
        raise InvalidEvent(f"{field}_invalid") from None


def _object(value: object, field: str, fields: frozenset[str]) -> dict[str, Any]:
    """Objeto con exactamente los campos del contrato (`additionalProperties: false`)."""
    if not isinstance(value, dict):
        raise InvalidEvent(f"{field}_not_object")
    if set(value) != fields:
        raise InvalidEvent(f"{field}_fields_mismatch")
    return value


def _envelope(envelope: dict[str, Any], schema_version: int) -> tuple[UUID, UUID, int]:
    """Valida el sobre de CMP-001; devuelve `event_id`, `aggregate_id` y versión."""
    if envelope.get("schema_version") != schema_version:
        raise InvalidEvent("unsupported_schema_version")
    if envelope.get("producer") != PRODUCER:
        raise InvalidEvent("producer_mismatch")
    event_id = _uuid(envelope.get("event_id"), "event_id")
    aggregate_id = _uuid(envelope.get("aggregate_id"), "aggregate_id")
    version = envelope.get("aggregate_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise InvalidEvent("aggregate_version_invalid")
    _aware_datetime(envelope.get("occurred_at"), "occurred_at")
    causation = envelope.get("causation_id")
    if causation is not None and not isinstance(causation, str):
        raise InvalidEvent("causation_id_invalid")
    return event_id, aggregate_id, version


def _correlation(envelope: dict[str, Any]) -> UUID | None:
    correlation = envelope.get("correlation_id")
    return _uuid(correlation, "correlation_id") if correlation is not None else None


def _user_registered(envelope: dict[str, Any]) -> UserRegisteredEvent:
    event_id, aggregate_id, version = _envelope(envelope, UserRegistered.SCHEMA_VERSION)
    if version != REGISTERED_VERSION:
        raise InvalidEvent("aggregate_version_invalid")
    payload = _object(envelope.get("payload"), "payload", _REGISTERED_FIELDS)
    if _uuid(payload["user_id"], "user_id") != aggregate_id:
        raise InvalidEvent("aggregate_id_mismatch")
    full_name = _text(payload["full_name"], "full_name", 1, 120)
    email = _text(payload["email"], "email", 3, 254)
    actor_type = _choice(
        payload["actor_type"], "actor_type", frozenset(value.value for value in ActorType)
    )
    # El registro siempre crea la cuenta activa (esquema: `enum: ["active"]`).
    status = _choice(payload["status"], "status", frozenset({AccountStatus.ACTIVE.value}))
    _aware_datetime(payload["registered_at"], "registered_at")
    return UserRegisteredEvent(
        event_id=event_id,
        user_id=aggregate_id,
        aggregate_version=version,
        correlation_id=_correlation(envelope),
        full_name=full_name,
        email=email,
        actor_type=actor_type,
        status=status,
    )


def _validate_demographic(payload: dict[str, Any]) -> None:
    _date(payload["birth_date"], "birth_date")
    _choice(payload["gender"], "gender", GENDERS)
    _matches(payload["country_code"], "country_code", _COUNTRY)
    _text(payload["city"], "city", 2, 80)
    _optional(payload["marital_status"], lambda v: _choice(v, "marital_status", MARITAL_STATUSES))
    _optional(payload["occupation"], lambda v: _text(v, "occupation", 2, 80))
    _optional(payload["income_range"], lambda v: _choice(v, "income_range", INCOME_RANGES))


def _validate_blood_pressure(value: object) -> None:
    pressure = _object(value, "blood_pressure", _BLOOD_PRESSURE_FIELDS)
    _int(pressure["systolic_mmhg"], "systolic_mmhg", 60, 250)
    _int(pressure["diastolic_mmhg"], "diastolic_mmhg", 30, 150)


def _validate_physiological(payload: dict[str, Any]) -> None:
    consent = _object(payload["consent"], "consent", _CONSENT_FIELDS)
    _text(consent["version"], "consent_version", 1, 64)
    _instant(consent["accepted_at"], "consent_accepted_at")
    _instant(payload["measured_at"], "measured_at", _UTC_SECONDS)
    _matches(payload["weight_kg"], "weight_kg", _ONE_DECIMAL)
    _int(payload["height_cm"], "height_cm", 100, 250)
    _matches(payload["bmi"], "bmi", _ONE_DECIMAL)
    _optional(
        payload["resting_heart_rate_bpm"], lambda v: _int(v, "resting_heart_rate_bpm", 30, 220)
    )
    _optional(payload["blood_pressure"], _validate_blood_pressure)
    _optional(
        payload["body_temperature_c"], lambda v: _matches(v, "body_temperature_c", _ONE_DECIMAL)
    )


def _catalog_list(value: object, field: str, catalog: type[StrEnum], maximum: int) -> list[str]:
    """Lista de 1 a `maximum` valores únicos del catálogo en su orden canónico."""
    order = [member.value for member in catalog]
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= maximum
        or any(not isinstance(item, str) or item not in order for item in value)
        or value != sorted(set(value), key=order.index)
    ):
        raise InvalidEvent(f"{field}_invalid")
    return value


def _validate_sport(value: object) -> str:
    sport = _object(value, "sport", _SPORT_FIELDS)
    _int(sport["frequency_per_week"], "frequency_per_week", FREQUENCY_MIN, FREQUENCY_MAX)
    _int(sport["session_duration_minutes"], "session_duration_minutes", DURATION_MIN, DURATION_MAX)
    _choice(sport["intensity"], "intensity", frozenset(Intensity))
    _choice(sport["level"], "level", frozenset(Level))
    _optional(
        sport["years_practicing"], lambda v: _int(v, "years_practicing", YEARS_MIN, YEARS_MAX)
    )
    return _choice(sport["sport"], "sport", frozenset(Sport))


def _validate_sport_habits(payload: dict[str, Any]) -> None:
    sports = payload["sports"]
    if not isinstance(sports, list) or not 1 <= len(sports) <= SPORTS_MAX:
        raise InvalidEvent("sports_invalid")
    names = [_validate_sport(item) for item in sports]
    order = [member.value for member in Sport]
    if names != sorted(set(names), key=order.index):
        raise InvalidEvent("sports_not_canonical")
    availability = _object(payload["availability"], "availability", _AVAILABILITY_FIELDS)
    _catalog_list(availability["days"], "days", Day, DAYS_MAX)
    _catalog_list(availability["time_slots"], "time_slots", TimeSlot, TIME_SLOTS_MAX)
    _optional(payload["primary_goal"], lambda v: _choice(v, "primary_goal", frozenset(PrimaryGoal)))


_SECTION_RULES: dict[ProfileSection, tuple[frozenset[str], Callable[[dict[str, Any]], None]]] = {
    ProfileSection.DEMOGRAPHIC: (DEMOGRAPHIC_FIELDS, _validate_demographic),
    ProfileSection.PHYSIOLOGICAL: (PHYSIOLOGICAL_FIELDS, _validate_physiological),
    ProfileSection.SPORT_HABITS: (SPORT_HABITS_FIELDS, _validate_sport_habits),
}


def _profile_section_updated(
    envelope: dict[str, Any], event_type: str
) -> ProfileSectionUpdatedEvent:
    section = SECTION_BY_EVENT_TYPE[event_type]
    event_id, aggregate_id, version = _envelope(envelope, PROFILE_SCHEMA_VERSION)
    fields, validate = _SECTION_RULES[section]
    payload = _object(envelope.get("payload"), "payload", fields)
    if _uuid(payload["user_id"], "user_id") != aggregate_id:
        raise InvalidEvent("aggregate_id_mismatch")
    if _int(payload["version"], "version", 1) != version:
        raise InvalidEvent("version_mismatch")
    _instant(payload["created_at"], "created_at")
    _instant(payload["updated_at"], "updated_at")
    validate(payload)
    return ProfileSectionUpdatedEvent(
        event_id=event_id,
        event_type=event_type,
        section=section,
        user_id=aggregate_id,
        aggregate_version=version,
        correlation_id=_correlation(envelope),
        # La vista guarda la sección tal como la devuelve el comando, sin `user_id`.
        document={key: value for key, value in payload.items() if key != "user_id"},
    )


def parse_users_event(envelope: object) -> UsersEvent:
    if not isinstance(envelope, dict):
        raise InvalidEvent("envelope_not_object")
    event_type = envelope.get("event_type")
    if not isinstance(event_type, str):
        raise InvalidEvent("event_type_missing")
    if event_type == UserRegistered.EVENT_TYPE:
        return _user_registered(envelope)
    if event_type in SECTION_BY_EVENT_TYPE:
        return _profile_section_updated(envelope, event_type)
    raise UnsupportedEvent(event_type)
