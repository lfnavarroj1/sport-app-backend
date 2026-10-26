"""Normalización `strava-activity.v1` de una actividad de Strava (HU031, ADR-042 §10).

Función pura: recibe el registro del proveedor tal como lo entregó el adaptador (que nunca
sale de este servicio) y devuelve el modelo neutral que viaja en `actividad.importada.v1`.

- `activity_id` = UUID v5 de `strava:<id>` en el espacio `NAMESPACE_URL` (R).
- `sport` por `sport_type` (o `type` si falta): `Run|TrailRun|VirtualRun → running`,
  `Ride|VirtualRide|MountainBikeRide|GravelRide → cycling`, `Swim → swimming`,
  `WeightTraining|Crossfit|Workout → strength`; el resto → `other`.
- `started_at` = `start_date` en UTC al segundo; `ended_at = started_at + elapsed_time`.
- `time_zone`: la zona IANA de `timezone` (`"(GMT-05:00) America/Bogota"`); `UTC` si no se
  reconoce (R).
- `distance_meters` y `elevation_gain_meters` redondeados al metro con `ROUND_HALF_UP`.
- `name` sin espacios externos y con 120 caracteres como máximo; vacío → `null`.

Obligatorios: `id`, `start_date`, `elapsed_time` (> 0), `distance` (≥ 0) y el tipo. Si
falta alguno o su tipo JSON no corresponde, la actividad se omite (`InvalidActivity`,
CA7). Un campo opcional (`moving_time`, `total_elevation_gain`, `name`, `timezone`) con
un valor inutilizable se trata como ausente (R).

Deduplicación (CA5): `external_version = "<activity_id>:<elapsed_time>:<distance>"` y
`payload_hash` = SHA-256 hexadecimal de ese texto; el mismo hash no publica nada y uno
distinto publica la versión siguiente.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROVIDER = "STRAVA"
MAPPING_VERSION = "strava-activity.v1"
NAME_MAX = 120
DEFAULT_TIME_ZONE = "UTC"
EXTERNAL_ID_MAX_DIGITS = 20


class Sport(StrEnum):
    RUNNING = "running"
    CYCLING = "cycling"
    SWIMMING = "swimming"
    STRENGTH = "strength"
    OTHER = "other"


SPORT_BY_TYPE: Mapping[str, Sport] = {
    "Run": Sport.RUNNING,
    "TrailRun": Sport.RUNNING,
    "VirtualRun": Sport.RUNNING,
    "Ride": Sport.CYCLING,
    "VirtualRide": Sport.CYCLING,
    "MountainBikeRide": Sport.CYCLING,
    "GravelRide": Sport.CYCLING,
    "Swim": Sport.SWIMMING,
    "WeightTraining": Sport.STRENGTH,
    "Crossfit": Sport.STRENGTH,
    "Workout": Sport.STRENGTH,
}


class SkipReason(StrEnum):
    """Motivo por el que se omitió una actividad; se registra sin datos del proveedor."""

    NOT_AN_OBJECT = "not_an_object"
    INVALID_ID = "invalid_id"
    INVALID_START_DATE = "invalid_start_date"
    INVALID_ELAPSED_TIME = "invalid_elapsed_time"
    INVALID_DISTANCE = "invalid_distance"
    INVALID_TYPE = "invalid_type"


class InvalidActivity(Exception):
    """La actividad está incompleta o mal formada: se omite y no se publica (CA7)."""

    code = "invalid_activity"

    def __init__(self, reason: SkipReason, external_activity_id: str | None) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.external_activity_id = external_activity_id


@dataclass(frozen=True)
class NormalizedActivity:
    activity_id: UUID
    external_activity_id: str
    sport: Sport
    name: str | None
    started_at: datetime
    ended_at: datetime
    time_zone: str
    duration_seconds: int
    moving_seconds: int | None
    distance_meters: int
    elevation_gain_meters: int | None
    external_version: str
    payload_hash: str


def activity_id_for(external_activity_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"strava:{external_activity_id}")


def _is_integer(value: object) -> bool:
    # `bool` es subclase de `int` en Python; en JSON son tipos distintos.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return _is_integer(value) or isinstance(value, float)


def round_meters(value: int | float) -> int:
    """Metros enteros con `ROUND_HALF_UP` (2.5 → 3); `Decimal(str(...))` evita el error
    binario de `float`."""
    return int(Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _external_id(record: Mapping[str, object]) -> str:
    value = record.get("id")
    if not _is_integer(value) or not isinstance(value, int) or value <= 0:
        raise InvalidActivity(SkipReason.INVALID_ID, None)
    text = str(value)
    if len(text) > EXTERNAL_ID_MAX_DIGITS:
        raise InvalidActivity(SkipReason.INVALID_ID, None)
    return text


def _start(record: Mapping[str, object], external_id: str) -> datetime:
    value = record.get("start_date")
    if not isinstance(value, str):
        raise InvalidActivity(SkipReason.INVALID_START_DATE, external_id)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise InvalidActivity(SkipReason.INVALID_START_DATE, external_id) from None
    if parsed.tzinfo is None:
        raise InvalidActivity(SkipReason.INVALID_START_DATE, external_id)
    return parsed.astimezone(UTC).replace(microsecond=0)


def _sport(record: Mapping[str, object], external_id: str) -> Sport:
    for field in ("sport_type", "type"):
        value = record.get(field)
        if isinstance(value, str) and value:
            return SPORT_BY_TYPE.get(value, Sport.OTHER)
    raise InvalidActivity(SkipReason.INVALID_TYPE, external_id)


def time_zone_from(value: object) -> str:
    """Zona IANA al final de `"(GMT-05:00) America/Bogota"`; `UTC` si no se reconoce."""
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_TIME_ZONE
    candidate = value.rsplit(")", 1)[-1].strip() if ")" in value else value.strip()
    if not candidate or " " in candidate:
        return DEFAULT_TIME_ZONE
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        return DEFAULT_TIME_ZONE
    return candidate


def name_from(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()[:NAME_MAX].strip()
    return trimmed or None


def _optional_seconds(value: object) -> int | None:
    return value if _is_integer(value) and isinstance(value, int) and value >= 0 else None


def _optional_meters(value: object) -> int | None:
    if not _is_number(value) or not isinstance(value, int | float) or value < 0:
        return None
    return round_meters(value)


def payload_hash_for(external_version: str) -> str:
    return hashlib.sha256(external_version.encode("utf-8")).hexdigest()


def normalize_activity(record: object) -> NormalizedActivity:
    """Aplica `strava-activity.v1`; lanza `InvalidActivity` si falta un dato obligatorio."""
    if not isinstance(record, Mapping):
        raise InvalidActivity(SkipReason.NOT_AN_OBJECT, None)
    external_id = _external_id(record)
    started_at = _start(record, external_id)
    elapsed = record.get("elapsed_time")
    if not _is_integer(elapsed) or not isinstance(elapsed, int) or elapsed <= 0:
        raise InvalidActivity(SkipReason.INVALID_ELAPSED_TIME, external_id)
    distance = record.get("distance")
    if not _is_number(distance) or not isinstance(distance, int | float) or distance < 0:
        raise InvalidActivity(SkipReason.INVALID_DISTANCE, external_id)
    sport = _sport(record, external_id)
    activity_id = activity_id_for(external_id)
    external_version = f"{activity_id}:{elapsed}:{distance}"
    return NormalizedActivity(
        activity_id=activity_id,
        external_activity_id=external_id,
        sport=sport,
        name=name_from(record.get("name")),
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=elapsed),
        time_zone=time_zone_from(record.get("timezone")),
        duration_seconds=elapsed,
        moving_seconds=_optional_seconds(record.get("moving_time")),
        distance_meters=round_meters(distance),
        elevation_gain_meters=_optional_meters(record.get("total_elevation_gain")),
        external_version=external_version,
        payload_hash=payload_hash_for(external_version),
    )


@dataclass(frozen=True)
class ImportedActivity:
    """Lo que el servicio recuerda de una actividad ya importada (deduplicación)."""

    external_activity_id: str
    user_id: UUID
    activity_id: UUID
    external_version: str
    payload_hash: str
    version: int
    started_at: datetime
    imported_at: datetime
    updated_at: datetime


def next_import(
    current: ImportedActivity | None, activity: NormalizedActivity, user_id: UUID, now: datetime
) -> ImportedActivity | None:
    """CA5: nueva → versión 1; mismo hash → `None` (nada que publicar); hash distinto →
    la versión siguiente."""
    if current is None:
        return ImportedActivity(
            external_activity_id=activity.external_activity_id,
            user_id=user_id,
            activity_id=activity.activity_id,
            external_version=activity.external_version,
            payload_hash=activity.payload_hash,
            version=1,
            started_at=activity.started_at,
            imported_at=now,
            updated_at=now,
        )
    if current.payload_hash == activity.payload_hash:
        return None
    return ImportedActivity(
        external_activity_id=current.external_activity_id,
        user_id=current.user_id,
        activity_id=current.activity_id,
        external_version=activity.external_version,
        payload_hash=activity.payload_hash,
        version=current.version + 1,
        started_at=activity.started_at,
        imported_at=current.imported_at,
        updated_at=now,
    )
