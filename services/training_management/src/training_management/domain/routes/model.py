"""Agregado `Route` y reglas de HU026 (ADR-041).

- Nombre de 3 a 120 caracteres y descripción opcional de hasta 2.000, recortados (el nombre
  además con los espacios internos colapsados); una descripción vacía queda en `None`.
- `sport` del catálogo de HU010; `difficulty` `LOW`, `MODERATE` o `HIGH`.
- `distance_km` decimal con un decimal entre `0.1` y `500.0`, normalizado a un decimal.
- Ciudad de 2 a 80 caracteres y país ISO 3166-1 alfa-2 asignado (reglas de ADR-028).
- Punto de inicio obligatorio y hasta 200 puntos intermedios opcionales, con `lat` entre
  -90 y 90 y `lng` entre -180 y 180. Sin proveedor cartográfico ni trazado GPX.
- Nace `PUBLISHED` en versión 1 (ADR-041: `POST /v1/routes` publica). La única transición
  es `PUBLISHED` → `RETIRED`, que sube una vez la versión; la fila se conserva (CA4).

La forma (tipos, longitudes y enumeraciones) la valida primero el modelo de transporte; aquí
se protegen las mismas reglas como invariantes del agregado, cada una con su campo y motivo.
"""

import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from uuid import UUID

from training_management.domain.plans.errors import Violation
from training_management.domain.plans.model import Sport
from training_management.domain.routes.countries import COUNTRY_CODES

NAME_MIN, NAME_MAX = 3, 120
DESCRIPTION_MAX = 2000
CITY_MIN, CITY_MAX = 2, 80
DISTANCE_MIN = Decimal("0.1")
DISTANCE_MAX = Decimal("500.0")
DISTANCE_QUANTUM = Decimal("0.1")
LATITUDE_LIMIT = 90.0
LONGITUDE_LIMIT = 180.0
WAYPOINTS_MAX = 200
INITIAL_VERSION = 1
# ADR-041: en el MVP todas las rutas las publican organizadores.
ROUTE_SOURCE = "ORGANIZER"

# Motivos de `details[].issue` que agrega el dominio a los de la validación de forma.
OUT_OF_RANGE = "out_of_range"
UNKNOWN_VALUE = "unknown_value"
INVALID_LENGTH = "invalid_length"


class Difficulty(StrEnum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


class RouteStatus(StrEnum):
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"


class InvalidRoute(Exception):
    """CA2: la ruta está incompleta o fuera de rango; informa todas las reglas incumplidas."""

    code = "validation_error"

    def __init__(self, violations: list[Violation]) -> None:
        super().__init__(", ".join(f"{item.field}: {item.issue}" for item in violations))
        self.violations = tuple(violations)


class RouteNotFound(Exception):
    """La ruta no existe o pertenece a otro organizador; no se revela cuál (CA4)."""

    code = "route_not_found"


class RouteVersionConflict(Exception):
    """`expected_version` no coincide con la versión vigente de la ruta publicada."""

    code = "version_conflict"

    def __init__(self, current_version: int) -> None:
        super().__init__(str(current_version))
        self.current_version = current_version


@dataclass(frozen=True)
class GeoPoint:
    lat: float
    lng: float


@dataclass(frozen=True)
class RouteInput:
    """Datos de la ruta con la forma JSON correcta."""

    name: str
    description: str | None
    sport: Sport
    distance_km: Decimal
    difficulty: Difficulty
    city: str
    country_code: str
    start_point: GeoPoint
    waypoints: tuple[GeoPoint, ...]


@dataclass(frozen=True)
class Route:
    route_id: UUID
    organizer_id: UUID
    name: str
    description: str | None
    sport: Sport
    distance_km: Decimal
    difficulty: Difficulty
    city: str
    country_code: str
    start_point: GeoPoint
    waypoints: tuple[GeoPoint, ...]
    status: RouteStatus
    version: int
    created_at: datetime
    updated_at: datetime
    published_at: datetime
    retired_at: datetime | None = None

    @classmethod
    def publish(
        cls, *, route_id: UUID, organizer_id: UUID, data: RouteInput, now: datetime
    ) -> "Route":
        """Crea la ruta `PUBLISHED` en versión 1, o falla con todas las reglas incumplidas."""
        name = " ".join(data.name.split())
        description = (data.description or "").strip() or None
        city = " ".join(data.city.split())
        violations = [
            *_text_violations(name, description, city),
            *_distance_violations(data.distance_km),
            *_country_violations(data.country_code),
            *_point_violations("start_point", data.start_point),
        ]
        if len(data.waypoints) > WAYPOINTS_MAX:
            violations.append(Violation("waypoints", OUT_OF_RANGE))
        for index, point in enumerate(data.waypoints):
            violations.extend(_point_violations(f"waypoints.{index}", point))
        if violations:
            raise InvalidRoute(violations)
        published_at = now.astimezone(UTC)
        return cls(
            route_id=route_id,
            organizer_id=organizer_id,
            name=name,
            description=description,
            sport=data.sport,
            distance_km=normalize_distance(data.distance_km),
            difficulty=data.difficulty,
            city=city,
            country_code=data.country_code,
            start_point=data.start_point,
            waypoints=data.waypoints,
            status=RouteStatus.PUBLISHED,
            version=INITIAL_VERSION,
            created_at=published_at,
            updated_at=published_at,
            published_at=published_at,
        )

    def retire(self, now: datetime) -> "Route":
        """`PUBLISHED` → `RETIRED`: una sola subida de versión; la ruta se conserva."""
        if self.status is not RouteStatus.PUBLISHED:
            raise ValueError("Solo una ruta publicada se puede retirar.")
        retired_at = now.astimezone(UTC)
        return replace(
            self,
            status=RouteStatus.RETIRED,
            version=self.version + 1,
            updated_at=retired_at,
            retired_at=retired_at,
        )


def distance_in_range(distance: Decimal) -> bool:
    """Decimal finito con como máximo un decimal, entre 0.1 y 500.0."""
    try:
        exponent = distance.as_tuple().exponent
    except (InvalidOperation, ValueError):
        return False
    if not isinstance(exponent, int) or not distance.is_finite():
        return False
    return exponent >= -1 and DISTANCE_MIN <= distance <= DISTANCE_MAX


def normalize_distance(distance: Decimal) -> Decimal:
    return distance.quantize(DISTANCE_QUANTUM)


def format_distance(distance: Decimal) -> str:
    """Cadena con exactamente un decimal (`12` → `"12.0"`)."""
    return format(normalize_distance(distance), "f")


def point_in_range(point: GeoPoint) -> bool:
    return all(
        math.isfinite(value) and -limit <= value <= limit
        for value, limit in ((point.lat, LATITUDE_LIMIT), (point.lng, LONGITUDE_LIMIT))
    )


def _text_violations(name: str, description: str | None, city: str) -> list[Violation]:
    violations: list[Violation] = []
    if not NAME_MIN <= len(name) <= NAME_MAX:
        violations.append(Violation("name", INVALID_LENGTH))
    if description is not None and len(description) > DESCRIPTION_MAX:
        violations.append(Violation("description", INVALID_LENGTH))
    if not CITY_MIN <= len(city) <= CITY_MAX:
        violations.append(Violation("city", INVALID_LENGTH))
    return violations


def _distance_violations(distance: Decimal) -> list[Violation]:
    return [] if distance_in_range(distance) else [Violation("distance_km", OUT_OF_RANGE)]


def _country_violations(country_code: str) -> list[Violation]:
    return [] if country_code in COUNTRY_CODES else [Violation("country_code", UNKNOWN_VALUE)]


def _point_violations(field: str, point: GeoPoint) -> list[Violation]:
    violations: list[Violation] = []
    if not (math.isfinite(point.lat) and -LATITUDE_LIMIT <= point.lat <= LATITUDE_LIMIT):
        violations.append(Violation(f"{field}.lat", OUT_OF_RANGE))
    if not (math.isfinite(point.lng) and -LONGITUDE_LIMIT <= point.lng <= LONGITUDE_LIMIT):
        violations.append(Violation(f"{field}.lng", OUT_OF_RANGE))
    return violations
