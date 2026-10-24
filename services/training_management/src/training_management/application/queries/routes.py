"""HU026: consultas de rutas sobre la base de lectura (`route_view`, ADR-041).

- `GET /v1/me/routes`: rutas del organizador autenticado en cualquier estado (permiso
  `training.routes.author`), orden `created_at` desc, `route_id` desc, con cursor opaco.
- `GET /v1/me/suggestions/routes`: sugerencias para el deportista con la capacidad
  `training.routes.suggest`. Los deportes, la ciudad y el país llegan como filtros explícitos
  (`bff-mobile` los obtiene de Usuarios): este dominio no conoce Usuarios. Las reglas de orden
  y los motivos están en `domain.routes.suggestions`.

Ambas son eventualmente consistentes con los comandos: una ruta recién publicada o retirada
se refleja cuando `entrenamientos-read-projector` aplica su evento.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from training_management.application.access import (
    require_athlete,
    require_route_author,
    require_route_suggest_capability,
)
from training_management.application.ports import (
    Caller,
    CapabilityChecker,
    MyRoutesQuery,
    PermissionPolicy,
    RoutePosition,
    RouteReader,
    RouteSuggestionQuery,
)
from training_management.domain.plans.model import Sport
from training_management.domain.routes.countries import COUNTRY_CODES
from training_management.domain.routes.model import CITY_MAX, CITY_MIN, Route
from training_management.domain.routes.suggestions import (
    SuggestionContext,
    SuggestionReason,
    reasons_for,
    suggestion_limit,
)

DEFAULT_LIMIT = 20
MAX_LIMIT = 50


class InvalidRouteQuery(Exception):
    """Parámetro de consulta inválido; se informa el parámetro afectado."""

    code = "validation_error"

    def __init__(self, field: str, issue: str) -> None:
        super().__init__(f"{field}: {issue}")
        self.field = field
        self.issue = issue


@dataclass(frozen=True)
class MyRoutesFilters:
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None


@dataclass(frozen=True)
class RoutePage:
    items: tuple[Route, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class SuggestionFilters:
    sports: tuple[Sport, ...] = ()
    city: str | None = None
    country_code: str | None = None
    limit: int = DEFAULT_LIMIT


@dataclass(frozen=True)
class RouteSuggestion:
    route: Route
    reasons: tuple[SuggestionReason, ...]


@dataclass(frozen=True)
class RouteSuggestions:
    items: tuple[RouteSuggestion, ...]
    personalized: bool


def encode_cursor(position: RoutePosition) -> str:
    raw = json.dumps(
        {"c": position.created_at.isoformat(), "r": str(position.route_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> RoutePosition:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        created_at = datetime.fromisoformat(document["c"])
        route_id = UUID(document["r"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        raise InvalidRouteQuery("cursor", "invalid_cursor") from None
    if created_at.tzinfo is None:
        raise InvalidRouteQuery("cursor", "invalid_cursor")
    return RoutePosition(created_at=created_at, route_id=route_id)


def _require_limit(limit: int) -> None:
    if not 1 <= limit <= MAX_LIMIT:
        raise InvalidRouteQuery("limit", "out_of_range")


class ListMyRoutes:
    def __init__(self, *, reader: RouteReader, permissions: PermissionPolicy) -> None:
        self._reader = reader
        self._permissions = permissions

    def __call__(self, caller: Caller, filters: MyRoutesFilters) -> RoutePage:
        require_route_author(caller, self._permissions)
        _require_limit(filters.limit)
        after = decode_cursor(filters.cursor) if filters.cursor else None
        # Se pide una de más para saber si existe una página siguiente.
        routes = self._reader.list_by_organizer(
            MyRoutesQuery(organizer_id=caller.user_id, limit=filters.limit + 1, after=after)
        )
        page = routes[: filters.limit]
        next_cursor = (
            encode_cursor(RoutePosition(page[-1].created_at, page[-1].route_id))
            if len(routes) > filters.limit
            else None
        )
        return RoutePage(items=tuple(page), next_cursor=next_cursor)


class SuggestRoutes:
    def __init__(self, *, reader: RouteReader, capabilities: CapabilityChecker) -> None:
        self._reader = reader
        self._capabilities = capabilities

    def __call__(self, caller: Caller, filters: SuggestionFilters) -> RouteSuggestions:
        require_athlete(caller)
        context = _context(filters)
        _require_limit(filters.limit)
        # Afiliación se consulta solo con una solicitud válida y antes de leer.
        require_route_suggest_capability(caller, self._capabilities)
        routes = self._reader.suggest(
            RouteSuggestionQuery(context=context, limit=suggestion_limit(context, filters.limit))
        )
        return RouteSuggestions(
            items=tuple(
                RouteSuggestion(
                    route=route,
                    reasons=reasons_for(context, city=route.city, country_code=route.country_code),
                )
                for route in routes
            ),
            personalized=context.personalized,
        )


def _context(filters: SuggestionFilters) -> SuggestionContext:
    city = " ".join(filters.city.split()) if filters.city is not None else None
    if city is not None and not CITY_MIN <= len(city) <= CITY_MAX:
        raise InvalidRouteQuery("city", "invalid_length")
    if filters.country_code is not None and filters.country_code not in COUNTRY_CODES:
        raise InvalidRouteQuery("country_code", "unknown_value")
    if len(set(filters.sports)) != len(filters.sports):
        raise InvalidRouteQuery("sport", "duplicate_value")
    return SuggestionContext(sports=filters.sports, city=city, country_code=filters.country_code)
