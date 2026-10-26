"""HU026 en el canal web: el organizador publica, retira y lista rutas (ADR-041).

El BFF autentica (JWKS), aplica el filtro de borde (`organizer` con `training.routes.author`
de `actor-permissions.v2`) y reenvía al servicio propietario con el token del usuario. Las
reglas de la ruta (distancia, país, coordenadas), la propiedad, la versión y la idempotencia
viven en `training_management`, que vuelve a verificarlo todo: el BFF entrega sus respuestas
sin alterarlas. Sin mapa ni proveedor cartográfico: los puntos son coordenadas.
"""

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from bff_web.api.training import (
    _UNAUTHORIZED,
    _UPSTREAM,
    IDEMPOTENCY_HEADER,
    REPLAYED_HEADER,
    _passthrough,
    _permission_required,
    _upstream_failure,
    authenticate_session,
)
from bff_web.clients.training_management import TrainingManagementClient
from bff_web.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.authorization import ActorPermissions
from sportapp_platform.errors import ErrorResponse
from sportapp_platform.tokens import VerifiedAccessToken

ROUTE_AUTHOR_PERMISSION = "training.routes.author"
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
DISTANCE_DESCRIPTION = (
    "Kilómetros como cadena decimal con un decimal entre `0.1` y `500.0`; se normaliza a un "
    "decimal (`12` → `12.0`). Fuera de rango o con más de un decimal: `out_of_range`."
)


# Enumeraciones con el mismo nombre y valores que el OpenAPI del servicio (prueba de contrato).
class Sport(StrEnum):
    RUNNING = "running"
    CYCLING = "cycling"
    SWIMMING = "swimming"
    STRENGTH = "strength"
    OTHER = "other"


class Difficulty(StrEnum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


Latitude = Annotated[float, Field(ge=-90.0, le=90.0, strict=True)]
Longitude = Annotated[float, Field(ge=-180.0, le=180.0, strict=True)]


class RoutePointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lat: Latitude
    lng: Longitude


# Mismo cuerpo que el servicio (prueba de contrato); las reglas las evalúa el servicio.
class PublishRouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=120)]
    description: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None
    ) = Field(default=None, description="Opcional; vacía equivale a `null`.")
    sport: Annotated[Sport, Field(description="Catálogo de deportes de HU010.")]
    distance_km: Annotated[
        str,
        StringConstraints(pattern=r"^[0-9]{1,8}(\.[0-9]{1,8})?$", max_length=17),
        Field(description=DISTANCE_DESCRIPTION, examples=["12.5"]),
    ]
    difficulty: Difficulty
    city: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=80)]
    country_code: Annotated[
        str,
        StringConstraints(pattern=r"^[A-Z]{2}$"),
        Field(description="ISO 3166-1 alfa-2 asignado, en mayúsculas.", examples=["CO"]),
    ]
    start_point: RoutePointRequest
    waypoints: Annotated[
        list[RoutePointRequest],
        Field(
            max_length=200,
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


class RoutesResponse(BaseModel):
    items: list[RouteResponse]
    next_cursor: str | None = Field(
        description="Cursor opaco de la página siguiente; `null` si no hay más."
    )


def _error(status: HTTPStatus, description: str) -> dict[int | str, dict[str, Any]]:
    return {status.value: {"model": ErrorResponse, "description": description}}


_FORBIDDEN = _error(
    HTTPStatus.FORBIDDEN,
    f"`permission_required` (`{ROUTE_AUTHOR_PERMISSION}`, solo organizador).",
)


def build_training_routes_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    permissions: ActorPermissions,
    training: TrainingManagementClient,
    channel: str,
) -> APIRouter:
    router = APIRouter(tags=["routes"])

    def author_token(authorization: str | None) -> str | Response:
        """Borde: token válido y permiso `training.routes.author` en el canal."""
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        actor = session.actor_type
        if actor is None or ROUTE_AUTHOR_PERMISSION not in permissions.for_actor(actor, channel):
            return _permission_required()
        return session.token

    @router.post(
        "/v1/routes",
        status_code=HTTPStatus.CREATED,
        response_model=RouteResponse,
        summary="Publicar una ruta (HU026)",
        description="La ruta nace `PUBLISHED` en versión 1. Reintentar con la misma "
        "`Idempotency-Key` (24 horas) devuelve la misma ruta sin publicar otra.",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            HTTPStatus.CREATED.value: {
                "headers": {
                    "Location": {"schema": {"type": "string"}},
                    REPLAYED_HEADER: {
                        "description": "`true` cuando se devuelve el resultado de un reintento.",
                        "schema": {"type": "string"},
                    },
                }
            },
            **_FORBIDDEN,
            **_error(HTTPStatus.CONFLICT, "`idempotency_key_reused`."),
            **_error(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "`validation_error` con `details[{field, issue}]` (forma del cuerpo y reglas de "
                "la ruta del servicio, sin alterar). Nada se publica.",
            ),
        },
    )
    def publish_route(
        body: PublishRouteRequest,
        idempotency_key: Annotated[UUID, Header(alias=IDEMPOTENCY_HEADER)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = author_token(authorization)
        if isinstance(token, Response):
            return token
        try:
            upstream = training.publish_route(token, body.model_dump(mode="json"), idempotency_key)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        return _passthrough(
            upstream,
            {
                HTTPStatus.CREATED,
                HTTPStatus.FORBIDDEN,
                HTTPStatus.CONFLICT,
                HTTPStatus.UNPROCESSABLE_ENTITY,
            },
            headers=("Location", REPLAYED_HEADER),
        )

    @router.post(
        "/v1/routes/{route_id}/retire",
        response_model=RouteResponse,
        summary="Retirar una ruta propia (HU026)",
        description="`PUBLISHED` → `RETIRED` con la versión siguiente; la ruta se conserva y "
        "deja de sugerirse. Una ruta ya retirada se devuelve tal cual.",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            **_FORBIDDEN,
            **_error(
                HTTPStatus.NOT_FOUND, "`route_not_found`: no existe o es de otro organizador."
            ),
            **_error(
                HTTPStatus.CONFLICT,
                "`version_conflict`: `details[0]` = `{field: expected_version, issue: <versión "
                "vigente>}`.",
            ),
            **_error(
                HTTPStatus.UNPROCESSABLE_ENTITY, "`validation_error`: `body.expected_version`."
            ),
        },
    )
    def retire_route(
        route_id: UUID,
        body: RetireRouteRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = author_token(authorization)
        if isinstance(token, Response):
            return token
        try:
            upstream = training.retire_route(token, route_id, body.model_dump(mode="json"))
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        return _passthrough(
            upstream,
            {
                HTTPStatus.OK,
                HTTPStatus.FORBIDDEN,
                HTTPStatus.NOT_FOUND,
                HTTPStatus.CONFLICT,
                HTTPStatus.UNPROCESSABLE_ENTITY,
            },
        )

    @router.get(
        "/v1/me/routes",
        response_model=RoutesResponse,
        summary="Mis rutas (HU026, lectura CQRS)",
        description="Rutas publicadas y retiradas del organizador, orden `created_at` y "
        "`route_id` descendente; el cursor es opaco.",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            **_FORBIDDEN,
            **_error(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "`validation_error` del servicio en `query.limit` (`out_of_range`, también si "
                "no es entero) o `query.cursor` (`invalid_cursor`), sin alterar.",
            ),
        },
    )
    def my_routes(
        *,
        limit: Annotated[
            str | None,
            Query(description=f"Entero de 1 a {MAX_LIMIT}; {DEFAULT_LIMIT} por defecto."),
        ] = None,
        cursor: Annotated[str | None, Query(description="`next_cursor` anterior.")] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = author_token(authorization)
        if isinstance(token, Response):
            response = token
        else:
            # El servicio valida `limit` y `cursor` con sus motivos estables.
            params = {
                name: value for name, value in (("limit", limit), ("cursor", cursor)) if value
            }
            try:
                upstream = training.list_my_routes(token, params)
            except (UpstreamTimeout, UpstreamUnavailable) as error:
                response = _upstream_failure(error)
            else:
                response = _passthrough(
                    upstream,
                    {HTTPStatus.OK, HTTPStatus.FORBIDDEN, HTTPStatus.UNPROCESSABLE_ENTITY},
                )
        response.headers["Cache-Control"] = "no-store"
        return response

    return router
