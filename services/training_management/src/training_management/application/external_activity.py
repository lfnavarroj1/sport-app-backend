"""HU031 (ADR-042 §11-§12): `actividad.importada.v1`, tal como lo consume Entrenamientos.

El contrato con `third_party_management` es solo el JSON del evento
(`contracts/events/terceros/actividad.importada.v1.schema.json`); aquí no se importa nada de
ese servicio. Se validan el sobre y el payload **exactos**: falta o sobra un campo → inválido.

- Otro `event_type` del tópico → `UnsupportedEvent` (se confirma sin efectos).
- `schema_version = 1`, `mapping_version = strava-activity.v1`, `provider = STRAVA`,
  `producer = third-party-management`, `aggregate_type = external_activity`,
  `aggregate_id == activity_id`, `aggregate_version == version`.
- Instantes en UTC con sufijo `Z`; `ended_at ≥ started_at`; `sport` del catálogo `Sport`.
- Enteros sin booleanos; los que van a columnas `INTEGER` no superan 2**31 - 1 (decisión de la
  tarea: un valor mayor fallaría en PostgreSQL y reintentaría hasta la DLQ; se rechaza antes).
- El nombre de la actividad se valida pero **no se conserva**: la proyección no lo usa, y así
  nunca llega a registros, errores ni a la base de lectura. Los motivos de `InvalidEvent` son
  códigos fijos, sin valores del mensaje.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from training_management.application.messages import InvalidEvent, UnsupportedEvent
from training_management.application.ports import Transaction
from training_management.domain.plans.model import Sport

EVENT_TYPE = "actividad.importada.v1"
SCHEMA_VERSION = 1
MAPPING_VERSION = "strava-activity.v1"
PROVIDER = "STRAVA"
PRODUCER = "third-party-management"
AGGREGATE_TYPE = "external_activity"

NAME_MAX_LENGTH = 120
_INT32_MAX = 2_147_483_647
_BIGINT_MAX = 9_223_372_036_854_775_807
# RFC 3339 en UTC: `YYYY-MM-DDTHH:MM:SS[.ffffff]Z`.
_UTC_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
_EXTERNAL_ID = re.compile(r"^[0-9]{1,20}$")
_SPORTS = frozenset(sport.value for sport in Sport)

ENVELOPE_FIELDS = frozenset(
    {
        "event_id",
        "event_type",
        "occurred_at",
        "producer",
        "aggregate_type",
        "aggregate_id",
        "aggregate_version",
        "correlation_id",
        "causation_id",
        "schema_version",
        "payload",
    }
)
PAYLOAD_FIELDS = frozenset(
    {
        "activity_id",
        "user_id",
        "provider",
        "external_activity_id",
        "sport",
        "name",
        "started_at",
        "ended_at",
        "time_zone",
        "duration_seconds",
        "moving_seconds",
        "distance_meters",
        "elevation_gain_meters",
        "mapping_version",
        "version",
    }
)
# (campo, valor exacto, motivo): el tipo también debe coincidir (`True` no vale por `1`).
_ENVELOPE_CONSTANTS: tuple[tuple[str, object, str], ...] = (
    ("schema_version", SCHEMA_VERSION, "unsupported_schema_version"),
    ("producer", PRODUCER, "unknown_producer"),
    ("aggregate_type", AGGREGATE_TYPE, "unknown_aggregate_type"),
)
_PAYLOAD_CONSTANTS: tuple[tuple[str, object, str], ...] = (
    ("mapping_version", MAPPING_VERSION, "unsupported_mapping_version"),
    ("provider", PROVIDER, "unknown_provider"),
)


@dataclass(frozen=True)
class ExternalActivity:
    """Actividad importada lista para `training_session_history` (sin nombre ni JSON externo)."""

    activity_id: UUID
    user_id: UUID
    sport: Sport
    started_at: datetime
    ended_at: datetime
    duration_seconds: int
    distance_meters: int
    version: int


@dataclass(frozen=True)
class ExternalActivityImported:
    event_id: UUID
    correlation_id: UUID | None
    causation_id: UUID
    activity: ExternalActivity


class ExternalActivityUnitOfWork(Transaction, Protocol):
    """Transacción local del consumidor en la base de lectura (con `processed_event`).

    El puerto vive junto al contrato que proyecta para no mezclarlo con los del proyector de
    `sportapp.entrenamientos.v1` (`ProjectionUnitOfWork`).
    """

    def is_processed(self, event_id: UUID) -> bool: ...

    def save_external_session(self, activity: ExternalActivity, projected_at: datetime) -> bool:
        """Inserta la fila si falta o la reemplaza si `version` es mayor que la proyectada;
        devuelve `False` (sin cambios) si la proyectada es igual o mayor."""
        ...

    def add_processed(
        self, event_id: UUID, event_type: str, aggregate_id: UUID, processed_at: datetime
    ) -> None: ...

    def commit(self) -> None:
        """Traduce un `event_id` ya registrado por otra entrega a `DuplicateDelivery`."""
        ...


def _uuid(value: object, field: str) -> UUID:
    if not isinstance(value, str):
        raise InvalidEvent(f"{field}_not_uuid")
    try:
        return UUID(value)
    except ValueError:
        raise InvalidEvent(f"{field}_not_uuid") from None


def _instant(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not _UTC_INSTANT.match(value):
        raise InvalidEvent(f"{field}_not_utc")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise InvalidEvent(f"{field}_not_utc") from None


def _integer(value: object, field: str, minimum: int, maximum: int = _INT32_MAX) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise InvalidEvent(f"{field}_out_of_range")
    return value


def _optional_integer(value: object, field: str) -> None:
    if value is not None:
        _integer(value, field, 0)


def _exact_fields(document: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    keys = set(document)
    if expected - keys:
        raise InvalidEvent(f"{label}_missing_fields")
    if keys - expected:
        raise InvalidEvent(f"{label}_unexpected_fields")


def _constants(document: Mapping[str, Any], constants: tuple[tuple[str, object, str], ...]) -> None:
    for field, expected, reason in constants:
        value = document[field]
        if type(value) is not type(expected) or value != expected:
            raise InvalidEvent(reason)


def _check_descriptive_fields(payload: Mapping[str, Any]) -> None:
    """Campos del contrato que la proyección no conserva, validados igualmente."""
    external_id = payload["external_activity_id"]
    if not isinstance(external_id, str) or not _EXTERNAL_ID.match(external_id):
        raise InvalidEvent("external_activity_id_invalid")
    name = payload["name"]
    if name is not None and (not isinstance(name, str) or not 1 <= len(name) <= NAME_MAX_LENGTH):
        raise InvalidEvent("name_invalid")
    time_zone = payload["time_zone"]
    if not isinstance(time_zone, str) or not time_zone:
        raise InvalidEvent("time_zone_invalid")
    _optional_integer(payload["moving_seconds"], "moving_seconds")
    _optional_integer(payload["elevation_gain_meters"], "elevation_gain_meters")


def _activity(payload: Mapping[str, Any]) -> ExternalActivity:
    _constants(payload, _PAYLOAD_CONSTANTS)
    _check_descriptive_fields(payload)
    sport = payload["sport"]
    if not isinstance(sport, str) or sport not in _SPORTS:
        raise InvalidEvent("unknown_sport")
    started_at = _instant(payload["started_at"], "started_at")
    ended_at = _instant(payload["ended_at"], "ended_at")
    if ended_at < started_at:
        raise InvalidEvent("ended_before_started")
    return ExternalActivity(
        activity_id=_uuid(payload["activity_id"], "activity_id"),
        user_id=_uuid(payload["user_id"], "user_id"),
        sport=Sport(sport),
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=_integer(payload["duration_seconds"], "duration_seconds", 1),
        distance_meters=_integer(payload["distance_meters"], "distance_meters", 0),
        version=_integer(payload["version"], "version", 1, _BIGINT_MAX),
    )


def parse_external_activity_event(envelope: object) -> ExternalActivityImported:
    """Valida el sobre y el payload de `actividad.importada.v1`.

    Lanza `UnsupportedEvent` para otro tipo del tópico e `InvalidEvent` si no cumple el
    contrato (el mensaje no se confirma y termina en la DLQ).
    """
    if not isinstance(envelope, dict):
        raise InvalidEvent("envelope_not_object")
    event_type = envelope.get("event_type")
    if not isinstance(event_type, str):
        raise InvalidEvent("event_type_missing")
    if event_type != EVENT_TYPE:
        raise UnsupportedEvent(event_type)
    _exact_fields(envelope, ENVELOPE_FIELDS, "envelope")
    _constants(envelope, _ENVELOPE_CONSTANTS)
    _instant(envelope["occurred_at"], "occurred_at")
    payload = envelope["payload"]
    if not isinstance(payload, dict):
        raise InvalidEvent("payload_not_object")
    _exact_fields(payload, PAYLOAD_FIELDS, "payload")
    activity = _activity(payload)
    if _uuid(envelope["aggregate_id"], "aggregate_id") != activity.activity_id:
        raise InvalidEvent("aggregate_id_mismatch")
    aggregate_version = _integer(envelope["aggregate_version"], "aggregate_version", 1, _BIGINT_MAX)
    if aggregate_version != activity.version:
        raise InvalidEvent("aggregate_version_mismatch")
    correlation = envelope["correlation_id"]
    return ExternalActivityImported(
        event_id=_uuid(envelope["event_id"], "event_id"),
        correlation_id=_uuid(correlation, "correlation_id") if correlation is not None else None,
        causation_id=_uuid(envelope["causation_id"], "causation_id"),
        activity=activity,
    )
