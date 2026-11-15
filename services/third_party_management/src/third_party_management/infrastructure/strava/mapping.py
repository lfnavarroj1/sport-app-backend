"""Traductor `strava-activity.v1` del adaptador de Strava (ADR-007, ADR-042 §10).

Es la única pieza que conoce el formato de una actividad de Strava (`id`, `sport_type`,
`start_date`, `timezone`, `elapsed_time`, `distance`…). Recibe el registro tal como lo
entregó la API y devuelve el modelo neutral del dominio (`NormalizedActivity`), que es lo
único que cruza el puerto `ActivityProviderGateway` hacia la aplicación.

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
falta alguno o su tipo JSON no corresponde, la actividad se rechaza (`InvalidActivity`,
CA7). Un campo opcional (`moving_time`, `total_elevation_gain`, `name`, `timezone`) con
un valor inutilizable se trata como ausente (R).

Deduplicación (CA5): `external_version = "<activity_id>:<elapsed_time>:<distance>"` y
`payload_hash` = SHA-256 hexadecimal de ese texto. `distance` se escribe siempre como número
decimal (`float`), para que `1000` y `1000.0` den el mismo hash.
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from third_party_management.domain.activities import (
    NAME_MAX,
    InvalidActivity,
    NormalizedActivity,
    SkipReason,
    Sport,
    activity_id_for,
    payload_hash_for,
)

DEFAULT_TIME_ZONE = "UTC"
EXTERNAL_ID_MAX_DIGITS = 20

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
    external_version = f"{activity_id}:{elapsed}:{float(distance)!r}"
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
