"""`ruta.publicada.v1` y `ruta.retirada.v1` en `sportapp.entrenamientos.v1` (ADR-041, HU026).

- Agregado `route`; `aggregate_id` y clave de orden: `route_id`; versión 1 al publicar y 2
  al retirar.
- `causation_id`: la `Idempotency-Key` al publicar; al retirar, el `route_id` (el retiro no
  lleva clave: lo protege `expected_version`).
- El payload es el estado completo de la ruta (el mismo documento de la respuesta), para que
  `entrenamientos-read-projector` construya `route_view` aplicando solo versiones mayores.
- Sin datos personales salvo `organizer_id`; instantes en UTC con sufijo `Z` y distancia
  como cadena con un decimal.
"""

from datetime import UTC, datetime
from typing import Any

from training_management.domain.routes.model import (
    ROUTE_SOURCE,
    GeoPoint,
    Route,
    RouteStatus,
    format_distance,
)

PUBLISHED_EVENT_TYPE = "ruta.publicada.v1"
RETIRED_EVENT_TYPE = "ruta.retirada.v1"
SCHEMA_VERSION = 1
AGGREGATE_TYPE = "route"
EVENT_TYPE_BY_STATUS = {
    RouteStatus.PUBLISHED: PUBLISHED_EVENT_TYPE,
    RouteStatus.RETIRED: RETIRED_EVENT_TYPE,
}


def utc_instant(value: datetime) -> str:
    """ISO 8601 en UTC con sufijo `Z`; la conversión a la zona del usuario es del cliente."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def point_document(point: GeoPoint) -> dict[str, float]:
    return {"lat": point.lat, "lng": point.lng}


def route_document(route: Route) -> dict[str, Any]:
    """Estado completo de la ruta: respuesta de la API, payload del evento y vista de lectura."""
    return {
        "route_id": str(route.route_id),
        "organizer_id": str(route.organizer_id),
        "name": route.name,
        "description": route.description,
        "sport": route.sport.value,
        "distance_km": format_distance(route.distance_km),
        "difficulty": route.difficulty.value,
        "city": route.city,
        "country_code": route.country_code,
        "start_point": point_document(route.start_point),
        "waypoints": [point_document(point) for point in route.waypoints],
        "source": ROUTE_SOURCE,
        "status": route.status.value,
        "version": route.version,
        "created_at": utc_instant(route.created_at),
        "updated_at": utc_instant(route.updated_at),
        "published_at": utc_instant(route.published_at),
        "retired_at": utc_instant(route.retired_at) if route.retired_at is not None else None,
    }
