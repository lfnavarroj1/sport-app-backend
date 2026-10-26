"""HU026 (ADR-041): rutas sugeribles.

- `POST /v1/routes`: el `organizer` con `training.routes.author` publica una ruta (`201` +
  `Location`); la misma clave devuelve el resultado original con `Idempotent-Replayed: true`.
- `POST /v1/routes/{route_id}/retire`: retira una ruta propia con `expected_version`; una ruta
  ajena o inexistente es `404 route_not_found`.
- `GET /v1/me/routes`: rutas del organizador autenticado con cursor opaco (lectura CQRS).
- `GET /v1/me/suggestions/routes`: sugerencias para el `athlete` con `training.routes.suggest`;
  `bff-mobile` envía los deportes, la ciudad y el país del deportista como filtros explícitos.

El organizador sale del token; el cuerpo nunca lo identifica. Ninguna operación integra un
proveedor cartográfico: los puntos son coordenadas validadas.
"""

from datetime import datetime
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from training_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    correlation_uuid,
    forbidden,
)
from training_management.application.access import (
    ROUTE_AUTHOR_PERMISSION,
    ROUTE_SUGGEST_CAPABILITY,
)
from training_management.application.commands.routes import (
    PublishRoute,
    PublishRouteCommand,
    RetireRoute,
    RetireRouteCommand,
)
from training_management.application.errors import (
    CapabilitiesTimeout,
    CapabilitiesUnavailable,
    CapabilityRequired,
    IdempotencyKeyReused,
    PermissionRequired,
)
from training_management.application.queries.routes import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    InvalidRouteQuery,
    ListMyRoutes,
    MyRoutesFilters,
    RoutePage,
    RouteSuggestions,
    SuggestionFilters,
    SuggestRoutes,
)
from training_management.domain.plans.model import Sport
from training_management.domain.routes.events import route_document
from training_management.domain.routes.model import (
    CITY_MAX,
    CITY_MIN,
    DESCRIPTION_MAX,
    LATITUDE_LIMIT,
    LONGITUDE_LIMIT,
    NAME_MAX,
    NAME_MIN,
    WAYPOINTS_MAX,
    Difficulty,
    GeoPoint,
    InvalidRoute,
    Route,
    RouteInput,
    RouteNotFound,
    RouteVersionConflict,
)
from training_management.domain.routes.suggestions import NON_PERSONALIZED_MAX

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
DISTANCE_DESCRIPTION = (
    "Kilómetros como cadena decimal con un decimal entre `0.1` y `500.0`; se normaliza a un "
    "decimal (`12` → `12.0`). Fuera de rango o con más de un decimal: `out_of_range`."
)

Latitude = Annotated[float, Field(ge=-LATITUDE_LIMIT, le=LATITUDE_LIMIT, strict=True)]
Longitude = Annotated[float, Field(ge=-LONGITUDE_LIMIT, le=LONGITUDE_LIMIT, strict=True)]


class RoutePointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lat: Latitude
    lng: Longitude


class PublishRouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=NAME_MIN, max_length=NAME_MAX)
    ]
    description: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=DESCRIPTION_MAX)] | None
    ) = Field(default=None, description="Opcional; vacía equivale a `null`.")
    sport: Annotated[Sport, Field(description="Catálogo de deportes de HU010.")]
    distance_km: Annotated[
        str,
        StringConstraints(pattern=r"^[0-9]{1,8}(\.[0-9]{1,8})?$", max_length=17),
        Field(description=DISTANCE_DESCRIPTION, examples=["12.5"]),
    ]
    difficulty: Difficulty
    city: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=CITY_MIN, max_length=CITY_MAX)
    ]
    country_code: Annotated[
        str,
        StringConstraints(pattern=r"^[A-Z]{2}$"),
        Field(description="ISO 3166-1 alfa-2 asignado, en mayúsculas.", examples=["CO"]),
    ]
    start_point: RoutePointRequest
    waypoints: Annotated[
        list[RoutePointRequest],
        Field(
            max_length=WAYPOINTS_MAX,
            description="Puntos intermedios opcionales, en orden; sin trazado GPX.",
        ),
    ] = []


class RetireRouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[
        int, Field(ge=1, strict=True, description="Versión vigente que conoce el cliente.")
    ]


class RoutePoint(BaseModel):
    lat: float
    lng: float


class RouteResponse(BaseModel):
    route_id: UUID
    organizer_id: UUID
    name: str
    description: str | None
    sport: Sport
    distance_km: Annotated[str, Field(description="Cadena con exactamente un decimal.")]
    difficulty: Difficulty
    city: str
    country_code: str
    start_point: RoutePoint
    waypoints: list[RoutePoint]
    source: Literal["ORGANIZER"]
    status: Literal["PUBLISHED", "RETIRED"]
    version: int
    created_at: datetime
    updated_at: datetime
    published_at: datetime
    retired_at: datetime | None

    @classmethod
    def from_route(cls, route: Route) -> "RouteResponse":
        return cls.model_validate(route_document(route))


class RoutesResponse(BaseModel):
    items: list[RouteResponse]
    next_cursor: str | None = Field(
        description="Cursor opaco de la página siguiente; `null` si no hay más."
    )

    @classmethod
    def from_page(cls, page: RoutePage) -> "RoutesResponse":
        return cls(
            items=[RouteResponse.from_route(route) for route in page.items],
            next_cursor=page.next_cursor,
        )


class RouteSuggestionResponse(RouteResponse):
    reasons: list[Literal["SPORT_MATCH", "CITY_MATCH"]] = Field(
        description="Motivos de la sugerencia; vacío en la lista no personalizada."
    )


class RouteSuggestionsResponse(BaseModel):
    items: list[RouteSuggestionResponse]
    personalized: bool = Field(
        description="`false` si no llegó ningún deporte: últimas publicadas, sin motivos."
    )

    @classmethod
    def from_result(cls, result: RouteSuggestions) -> "RouteSuggestionsResponse":
        return cls(
            items=[
                RouteSuggestionResponse.model_validate(
                    {
                        **route_document(item.route),
                        "reasons": [reason.value for reason in item.reasons],
                    }
                )
                for item in result.items
            ],
            personalized=result.personalized,
        )


def _point(point: RoutePointRequest) -> GeoPoint:
    return GeoPoint(lat=float(point.lat), lng=float(point.lng))


def _publish_command(body: PublishRouteRequest, key: UUID) -> PublishRouteCommand:
    return PublishRouteCommand(
        data=RouteInput(
            name=body.name,
            description=body.description,
            sport=body.sport,
            distance_km=Decimal(body.distance_km),
            difficulty=body.difficulty,
            city=body.city,
            country_code=body.country_code,
            start_point=_point(body.start_point),
            waypoints=tuple(_point(point) for point in body.waypoints),
        ),
        idempotency_key=key,
        correlation_id=correlation_uuid(),
    )


def _limit(raw: str | None) -> int:
    """`limit` como entero; uno no entero se informa como `out_of_range` al validarlo."""
    if raw is None:
        return DEFAULT_LIMIT
    try:
        return int(raw.strip())
    except ValueError:
        return 0


def _invalid_route(error: InvalidRoute) -> Response:
    return error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        error.code,
        "La ruta no es válida.",
        [ErrorDetail(field=f"body.{item.field}", issue=item.issue) for item in error.violations],
    )


def _invalid_query(error: InvalidRouteQuery) -> Response:
    return error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        error.code,
        "La consulta no es válida.",
        [ErrorDetail(field=f"query.{error.field}", issue=error.issue)],
    )


def _not_found() -> Response:
    return error_response(HTTPStatus.NOT_FOUND, RouteNotFound.code, "La ruta no existe.")


_AUTHOR_FORBIDDEN = {
    "model": ErrorResponse,
    "description": f"`permission_required` (`{ROUTE_AUTHOR_PERMISSION}`, solo organizador).",
}
_QUERY_ERRORS = {
    "model": ErrorResponse,
    "description": "`validation_error`: `query.limit` (`out_of_range`, también si no es un "
    "entero) o `query.cursor` (`invalid_cursor`).",
}


def build_routes_router(
    *,
    verify_token: TokenVerifier,
    publish: PublishRoute,
    retire: RetireRoute,
    list_routes: ListMyRoutes,
    suggest: SuggestRoutes,
) -> APIRouter:
    router = APIRouter(tags=["routes"])

    @router.post(
        "/v1/routes",
        status_code=HTTPStatus.CREATED,
        response_model=RouteResponse,
        summary="Publicar una ruta (HU026)",
        description="La ruta nace `PUBLISHED` en versión 1, sin moderación ni proveedor "
        "cartográfico. Reintentar con la misma `Idempotency-Key` (24 horas) devuelve la misma "
        "ruta sin publicar otra.",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.CREATED.value: {
                "headers": {
                    "Location": {
                        "description": "`/v1/routes/{route_id}`.",
                        "schema": {"type": "string"},
                    },
                    REPLAYED_HEADER: {
                        "description": "`true` cuando se devuelve el resultado de un reintento.",
                        "schema": {"type": "string"},
                    },
                }
            },
            HTTPStatus.FORBIDDEN.value: _AUTHOR_FORBIDDEN,
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`idempotency_key_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` con `details[{field, issue}]`: forma del "
                "cuerpo (campos faltantes, longitudes, enumeraciones, coordenadas fuera de "
                "rango) y reglas de la ruta (`body.distance_km` `out_of_range`, "
                "`body.country_code` `unknown_value`, `body.start_point.lat` `out_of_range`…). "
                "Nada se publica.",
            },
        },
    )
    def publish_route(
        body: PublishRouteRequest,
        response: Response,
        idempotency_key: Annotated[UUID, Header(alias=IDEMPOTENCY_HEADER)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | RouteResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            result = publish(caller, _publish_command(body, idempotency_key))
        except PermissionRequired as error:
            return forbidden(error)
        except InvalidRoute as error:
            return _invalid_route(error)
        except IdempotencyKeyReused as error:
            return error_response(
                HTTPStatus.CONFLICT,
                error.code,
                "La clave de idempotencia ya se usó con otra solicitud.",
            )
        response.headers["Location"] = f"/v1/routes/{result.route.route_id}"
        if result.replayed:
            response.headers[REPLAYED_HEADER] = "true"
        return RouteResponse.from_route(result.route)

    @router.post(
        "/v1/routes/{route_id}/retire",
        response_model=RouteResponse,
        summary="Retirar una ruta propia (HU026)",
        description="`PUBLISHED` → `RETIRED` con la versión siguiente; la ruta se conserva y "
        "deja de sugerirse. Una ruta ya retirada se devuelve tal cual (sin otro evento).",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.FORBIDDEN.value: _AUTHOR_FORBIDDEN,
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": "`route_not_found`: no existe o es de otro organizador.",
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`version_conflict`: `details[0]` = `{field: expected_version, "
                "issue: <versión vigente>}`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `body.expected_version`.",
            },
        },
    )
    def retire_route(
        route_id: UUID,
        body: RetireRouteRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | RouteResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            result = retire(
                caller,
                RetireRouteCommand(
                    route_id=route_id,
                    expected_version=body.expected_version,
                    correlation_id=correlation_uuid(),
                ),
            )
        except PermissionRequired as error:
            return forbidden(error)
        except RouteNotFound:
            return _not_found()
        except RouteVersionConflict as error:
            return error_response(
                HTTPStatus.CONFLICT,
                error.code,
                "La ruta cambió desde la versión indicada.",
                [ErrorDetail(field="expected_version", issue=str(error.current_version))],
            )
        return RouteResponse.from_route(result.route)

    @router.get(
        "/v1/me/routes",
        response_model=RoutesResponse,
        summary="Mis rutas (HU026, lectura CQRS)",
        description="Rutas publicadas y retiradas del organizador autenticado, orden "
        "`created_at` desc y `route_id` desc. Refleja un comando cuando el proyector aplica "
        "su evento.",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.FORBIDDEN.value: _AUTHOR_FORBIDDEN,
            HTTPStatus.UNPROCESSABLE_ENTITY.value: _QUERY_ERRORS,
        },
    )
    def my_routes(
        *,
        response: Response,
        limit: Annotated[
            str | None,
            Query(description=f"Entero de 1 a {MAX_LIMIT}; {DEFAULT_LIMIT} por defecto."),
        ] = None,
        cursor: Annotated[str | None, Query(description="`next_cursor` anterior.")] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | RoutesResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            page = list_routes(caller, MyRoutesFilters(limit=_limit(limit), cursor=cursor))
        except PermissionRequired as error:
            return forbidden(error)
        except InvalidRouteQuery as error:
            return _invalid_query(error)
        response.headers["Cache-Control"] = "no-store"
        return RoutesResponse.from_page(page)

    @router.get(
        "/v1/me/suggestions/routes",
        response_model=RouteSuggestionsResponse,
        summary="Rutas sugeridas para el deportista (HU026, lectura CQRS)",
        description="Rutas `PUBLISHED` de los deportes indicados: primero las de la ciudad "
        "(y país, si llega), luego por `distance_km` y `route_id` ascendentes, con `reasons`. "
        f"Sin `sport`: las últimas publicadas (como mucho {NON_PERSONALIZED_MAX}), "
        "`personalized: false` y sin motivos. Los filtros los envía `bff-mobile` con el "
        "contexto del deportista; nunca el cliente.",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` (no es deportista) o `capability_required` "
                f"(`{ROUTE_SUGGEST_CAPABILITY}`).",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `query.limit` (`out_of_range`), `query.sport` "
                "(valor desconocido o `duplicate_value`), `query.city` (`invalid_length`) o "
                "`query.country_code` (`unknown_value`).",
            },
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": "`upstream_unavailable`: no se pudo verificar identidad o plan.",
            },
            HTTPStatus.GATEWAY_TIMEOUT.value: {
                "model": ErrorResponse,
                "description": "`upstream_timeout`: Afiliación no respondió a tiempo.",
            },
        },
    )
    def route_suggestions(
        *,
        response: Response,
        sport: Annotated[
            list[Sport] | None, Query(description="Deportes del deportista (HU006); repetible.")
        ] = None,
        city: Annotated[
            str | None, Query(max_length=200, description="Ciudad del deportista (HU005).")
        ] = None,
        country_code: Annotated[
            str | None, Query(max_length=2, description="País del deportista (HU005).")
        ] = None,
        limit: Annotated[
            str | None,
            Query(description=f"Entero de 1 a {MAX_LIMIT}; {DEFAULT_LIMIT} por defecto."),
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | RouteSuggestionsResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        filters = SuggestionFilters(
            sports=tuple(sport or ()),
            city=city,
            country_code=country_code,
            limit=_limit(limit),
        )
        try:
            result = suggest(caller, filters)
        except InvalidRouteQuery as error:
            return _invalid_query(error)
        except (PermissionRequired, CapabilityRequired) as error:
            return forbidden(error)
        except CapabilitiesTimeout as error:
            return error_response(
                HTTPStatus.GATEWAY_TIMEOUT,
                error.code,
                "No fue posible verificar su plan a tiempo. Intente de nuevo.",
            )
        except CapabilitiesUnavailable as error:
            return error_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                error.code,
                "No fue posible verificar su plan. Intente de nuevo.",
            )
        response.headers["Cache-Control"] = "no-store"
        return RouteSuggestionsResponse.from_result(result)

    return router
