"""Reconstrucción validada de una ruta desde su documento de estado completo (HU026).

El mismo documento es la respuesta de la API, el resultado guardado para la idempotencia, el
payload de `ruta.publicada.v1`/`ruta.retirada.v1` y la fila de `route_view`. Un documento que
no cumple las reglas del agregado lanza `InvalidRouteDocument` con un motivo sin datos de la
ruta (el proyector lo usa como motivo de rechazo hacia la DLQ).
"""

import re
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from training_management.domain.plans.model import Sport
from training_management.domain.routes.countries import COUNTRY_CODES
from training_management.domain.routes.model import (
    CITY_MAX,
    CITY_MIN,
    DESCRIPTION_MAX,
    NAME_MAX,
    NAME_MIN,
    ROUTE_SOURCE,
    WAYPOINTS_MAX,
    Difficulty,
    GeoPoint,
    Route,
    RouteStatus,
    distance_in_range,
    point_in_range,
)

_DISTANCE_PATTERN = re.compile(r"[0-9]{1,3}\.[0-9]")


class InvalidRouteDocument(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _uuid(value: object, field: str) -> UUID:
    try:
        return UUID(str(value))
    except ValueError:
        raise InvalidRouteDocument(f"{field}_not_uuid") from None


def _instant(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        raise InvalidRouteDocument(f"{field}_not_datetime") from None
    if parsed.tzinfo is None:
        raise InvalidRouteDocument(f"{field}_without_offset")
    return parsed


def _text(value: object, field: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise InvalidRouteDocument(f"{field}_invalid")
    return value


def _point(value: object, field: str) -> GeoPoint:
    if not isinstance(value, dict):
        raise InvalidRouteDocument(f"{field}_invalid")
    lat, lng = value.get("lat"), value.get("lng")
    if not all(isinstance(item, int | float) and not isinstance(item, bool) for item in (lat, lng)):
        raise InvalidRouteDocument(f"{field}_invalid")
    point = GeoPoint(lat=float(lat), lng=float(lng))  # type: ignore[arg-type]
    if not point_in_range(point):
        raise InvalidRouteDocument(f"{field}_out_of_range")
    return point


def _enum[E: (Sport, Difficulty, RouteStatus)](kind: type[E], value: object, field: str) -> E:
    if value not in {item.value for item in kind}:
        raise InvalidRouteDocument(f"{field}_invalid")
    return kind(str(value))


def route_from_document(document: dict[str, Any]) -> Route:
    distance = document.get("distance_km")
    if not isinstance(distance, str) or not _DISTANCE_PATTERN.fullmatch(distance):
        raise InvalidRouteDocument("distance_km_invalid")
    if not distance_in_range(Decimal(distance)):
        raise InvalidRouteDocument("distance_km_invalid")
    description = document.get("description")
    if description is not None:
        description = _text(description, "description", 1, DESCRIPTION_MAX)
    country_code = document.get("country_code")
    if country_code not in COUNTRY_CODES:
        raise InvalidRouteDocument("country_code_invalid")
    waypoints = document.get("waypoints")
    if not isinstance(waypoints, list) or len(waypoints) > WAYPOINTS_MAX:
        raise InvalidRouteDocument("waypoints_invalid")
    if document.get("source") != ROUTE_SOURCE:
        raise InvalidRouteDocument("source_invalid")
    version = document.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise InvalidRouteDocument("version_invalid")
    status = _enum(RouteStatus, document.get("status"), "status")
    retired_raw = document.get("retired_at")
    retired_at = _instant(retired_raw, "retired_at") if retired_raw is not None else None
    if (status is RouteStatus.RETIRED) != (retired_at is not None):
        raise InvalidRouteDocument("retired_at_mismatch")
    return Route(
        route_id=_uuid(document.get("route_id"), "route_id"),
        organizer_id=_uuid(document.get("organizer_id"), "organizer_id"),
        name=_text(document.get("name"), "name", NAME_MIN, NAME_MAX),
        description=description,
        sport=_enum(Sport, document.get("sport"), "sport"),
        distance_km=Decimal(distance),
        difficulty=_enum(Difficulty, document.get("difficulty"), "difficulty"),
        city=_text(document.get("city"), "city", CITY_MIN, CITY_MAX),
        country_code=str(country_code),
        start_point=_point(document.get("start_point"), "start_point"),
        waypoints=tuple(_point(item, "waypoints") for item in waypoints),
        status=status,
        version=version,
        created_at=_instant(document.get("created_at"), "created_at"),
        updated_at=_instant(document.get("updated_at"), "updated_at"),
        published_at=_instant(document.get("published_at"), "published_at"),
        retired_at=retired_at,
    )
