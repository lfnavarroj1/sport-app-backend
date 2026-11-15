"""Modelo neutral de una actividad externa importada (HU031, ADR-007, ADR-042 §10-§11).

No conoce el formato de ningún proveedor: el adaptador de Strava
(`infrastructure/strava/mapping.py`, `strava-activity.v1`) traduce cada registro externo a
`NormalizedActivity`, que es lo único que cruza el puerto `ActivityProviderGateway` y lo
que viaja en `actividad.importada.v1`. Un registro que el adaptador no puede traducir se
rechaza con un `SkipReason` (CA7).

Deduplicación (CA5): el adaptador calcula `external_version` y su `payload_hash` (SHA-256);
el mismo hash no publica nada y uno distinto publica la versión siguiente (`next_import`).
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

PROVIDER = "STRAVA"
MAPPING_VERSION = "strava-activity.v1"
NAME_MAX = 120


class Sport(StrEnum):
    RUNNING = "running"
    CYCLING = "cycling"
    SWIMMING = "swimming"
    STRENGTH = "strength"
    OTHER = "other"


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
    """UUID v5 determinista de `strava:<id>` (ADR-042 §10)."""
    return uuid5(NAMESPACE_URL, f"strava:{external_activity_id}")


def payload_hash_for(external_version: str) -> str:
    return hashlib.sha256(external_version.encode("utf-8")).hexdigest()


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
