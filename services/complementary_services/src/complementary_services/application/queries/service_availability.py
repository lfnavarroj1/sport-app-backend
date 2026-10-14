"""HU020: disponibilidad de los servicios publicados para el deportista (ADR-038).

- Solo `athlete`. Periodo obligatorio `from`/`to` en instantes ISO 8601 con desfase, de
  hasta 31 días; se ofrecen los intervalos futuros que empiezan en `[from, to)`.
- Filtros opcionales y repetibles `category` y `modality`; `availability=available` (por
  defecto) excluye los servicios sin intervalos en el rango y `all` los incluye con
  `available: false`.
- `SPORTS_MEDICINE` solo con `services.sports_medicine.access`, consultada ante Afiliación
  únicamente si la categoría puede aparecer; sin la capacidad se omite y se declara en
  `restricted_categories` (también si se pidió explícitamente).
- Orden por `name` y `service_id` con cursor keyset opaco; `limit` de 1 a 50 (20 por
  defecto). La lectura no reserva ni bloquea cupos y no convierte zonas horarias.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from complementary_services.application.errors import PermissionRequired
from complementary_services.application.ports import (
    AvailabilityQuery,
    Caller,
    CapabilityChecker,
    CatalogPosition,
    Clock,
    ServiceCatalogReader,
)
from complementary_services.application.queries.my_services import InvalidServicesQuery
from complementary_services.domain.catalog.offer import Category, Modality, ServiceOffer
from complementary_services.domain.validation import Issue

ATHLETE = "athlete"
MEDICAL_CAPABILITY = "services.sports_medicine.access"
MAX_RANGE = timedelta(days=31)
MAX_LIMIT = 50
DEFAULT_LIMIT = 20
AVAILABLE = "available"
ALL = "all"


@dataclass(frozen=True)
class AvailabilityFilters:
    """Parámetros tal como llegan; `limit` ya es entero (el transporte informa si no lo es)."""

    starts_from: str | None
    starts_before: str | None
    categories: tuple[str, ...] = ()
    modalities: tuple[str, ...] = ()
    availability: str | None = None
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None


@dataclass(frozen=True)
class AvailabilityPage:
    items: tuple[ServiceOffer, ...]
    next_cursor: str | None
    restricted_categories: tuple[Category, ...]


def encode_cursor(position: CatalogPosition) -> str:
    raw = json.dumps(
        {"n": position.name, "s": str(position.service_id)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> CatalogPosition:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        name = document["n"]
        service_id = UUID(document["s"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        raise InvalidServicesQuery("cursor", Issue.UNKNOWN_VALUE) from None
    if not isinstance(name, str):
        raise InvalidServicesQuery("cursor", Issue.UNKNOWN_VALUE)
    return CatalogPosition(name=name, service_id=service_id)


def _instant(raw: str | None, field: str) -> datetime:
    if raw is None:
        raise InvalidServicesQuery(field, Issue.REQUIRED)
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        raise InvalidServicesQuery(field, Issue.UNKNOWN_VALUE) from None
    if value.tzinfo is None:
        raise InvalidServicesQuery(field, Issue.UNKNOWN_VALUE)
    return value


def _choices[E: Category | Modality](
    raw: tuple[str, ...], field: str, catalog: type[E]
) -> frozenset[E] | None:
    if not raw:
        return None
    try:
        return frozenset(catalog(value) for value in raw)
    except ValueError:
        raise InvalidServicesQuery(field, Issue.UNKNOWN_VALUE) from None


class ListServiceAvailability:
    def __init__(
        self,
        *,
        reader: ServiceCatalogReader,
        capabilities: CapabilityChecker,
        clock: Clock,
    ) -> None:
        self._reader = reader
        self._capabilities = capabilities
        self._clock = clock

    def authorize(self, caller: Caller) -> None:
        """Solo el deportista; también antes de leer los parámetros en el transporte."""
        if caller.actor_type != ATHLETE:
            raise PermissionRequired()

    def __call__(self, caller: Caller, filters: AvailabilityFilters) -> AvailabilityPage:
        self.authorize(caller)
        starts_from = _instant(filters.starts_from, "from")
        starts_before = _instant(filters.starts_before, "to")
        if starts_before <= starts_from:
            raise InvalidServicesQuery("to", Issue.ENDS_BEFORE_STARTS)
        if starts_before - starts_from > MAX_RANGE:
            raise InvalidServicesQuery("to", Issue.OUT_OF_RANGE)
        categories = _choices(filters.categories, "category", Category)
        modalities = _choices(filters.modalities, "modality", Modality)
        availability = filters.availability or AVAILABLE
        if availability not in {AVAILABLE, ALL}:
            raise InvalidServicesQuery("availability", Issue.UNKNOWN_VALUE)
        if not 1 <= filters.limit <= MAX_LIMIT:
            raise InvalidServicesQuery("limit", Issue.OUT_OF_RANGE)
        after = decode_cursor(filters.cursor) if filters.cursor is not None else None

        # Afiliación solo se consulta si la categoría médica puede aparecer, y nunca dentro de
        # una transacción: el lector abre su propia sesión después.
        restricted: tuple[Category, ...] = ()
        medical = Category.SPORTS_MEDICINE
        if (categories is None or medical in categories) and not self._capabilities.has_capability(
            caller.user_id, MEDICAL_CAPABILITY
        ):
            restricted = (medical,)
            categories = frozenset(categories or Category) - {medical}
            if not categories:
                return AvailabilityPage(
                    items=(), next_cursor=None, restricted_categories=restricted
                )

        offers = self._reader.published_availability(
            AvailabilityQuery(
                starts_from=starts_from,
                starts_before=starts_before,
                now=self._clock.now(),
                categories=categories,
                modalities=modalities,
                only_available=availability == AVAILABLE,
                after=after,
                limit=filters.limit + 1,
            )
        )
        page = tuple(offers[: filters.limit])
        next_cursor = (
            encode_cursor(CatalogPosition(page[-1].name, page[-1].service_id))
            if len(offers) > filters.limit
            else None
        )
        return AvailabilityPage(
            items=page, next_cursor=next_cursor, restricted_categories=restricted
        )
