"""HU026 en el canal móvil: rutas sugeridas para el deportista (ADR-041).

1. Borde: token (JWKS), actor `athlete` y capacidad vigente `training.routes.suggest`
   (Afiliación), como el resto de las opciones del plan.
2. Contexto: `GET /v1/users/{user_id}/athlete-context` (contrato interno) una vez por
   solicitud, con timeout de 300 ms y caché de 5 minutos por deportista (solo respuestas
   correctas). Si Usuarios no responde, la lista se entrega **no personalizada** con
   `missing_context` y la fuente `users` en `UNAVAILABLE` (HA011); nunca como «para ti».
3. Dominio: `training_management` recibe deportes, ciudad y país como filtros explícitos y
   aplica las reglas (deporte, ciudad, distancia, retiradas). Si falla, `503`/`504`.
4. Autoría: `author_name` por `POST /v1/users/directory-lookup` (ADR-039) con los
   organizadores de la página; sin respuesta, `null`.
Sugerir no inscribe ni asigna nada; el aviso de seguridad y la apertura del punto de inicio
con `geo:` son del cliente.
"""

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bff_mobile.api.training_metrics import (
    ATHLETE,
    BEARER_PREFIX,
    _failure,
    _passthrough,
    _unauthenticated,
)
from bff_mobile.clients.training_management import TrainingManagementClient
from bff_mobile.clients.users_management import (
    UpstreamTimeout,
    UpstreamUnavailable,
    UsersManagementClient,
)
from sportapp_platform.errors import ErrorResponse, error_response
from sportapp_platform.tokens import InvalidAccessToken, JwksUnavailable, VerifiedAccessToken

ROUTES_CAPABILITY = "training.routes.suggest"
CONTEXT_TIMEOUT_SECONDS = 0.3
CONTEXT_CACHE_SECONDS = 300.0
CONTEXT_CACHE_MAX_ENTRIES = 10_000
DEFAULT_LIMIT = 20
MAX_LIMIT = 50

Sport = Literal["running", "cycling", "swimming", "strength", "other"]
MissingContext = Literal["sports", "city"]


class AthleteContext(BaseModel):
    """Lo que las sugerencias usan del contrato interno de Usuarios (HU026 y HU028)."""

    sports: list[str]
    city: str | None
    country_code: str | None
    primary_goal: str | None = None


class AthleteContextCache:
    """Caché en memoria por deportista (5 minutos), acotada y segura entre hilos."""

    def __init__(
        self,
        *,
        ttl_seconds: float = CONTEXT_CACHE_SECONDS,
        max_entries: int = CONTEXT_CACHE_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._entries: dict[UUID, tuple[float, AthleteContext]] = {}
        self._lock = threading.Lock()

    def get(self, user_id: UUID) -> AthleteContext | None:
        with self._lock:
            entry = self._entries.get(user_id)
            if entry is None:
                return None
            if entry[0] <= self._clock():
                del self._entries[user_id]
                return None
            return entry[1]

    def put(self, user_id: UUID, context: AthleteContext) -> None:
        with self._lock:
            if len(self._entries) >= self._max and user_id not in self._entries:
                # Se descarta la entrada más antigua (orden de inserción).
                self._entries.pop(next(iter(self._entries)))
            self._entries[user_id] = (self._clock() + self._ttl, context)


class RoutePoint(BaseModel):
    lat: float
    lng: float


class SuggestionReference(BaseModel):
    type: Literal["route"]
    id: UUID


class RouteSuggestionItem(BaseModel):
    reference: SuggestionReference
    title: Annotated[str, Field(description="Nombre de la ruta.")]
    summary: Annotated[str | None, Field(description="Descripción de la ruta, si la tiene.")]
    reasons: list[Literal["SPORT_MATCH", "CITY_MATCH"]] = Field(
        description="Motivos de la sugerencia; vacío en la lista no personalizada."
    )
    route_id: UUID
    sport: Sport
    distance_km: Annotated[str, Field(description="Kilómetros con exactamente un decimal.")]
    difficulty: Literal["LOW", "MODERATE", "HIGH"]
    city: str
    country_code: str
    start_point: Annotated[
        RoutePoint, Field(description="Se abre en la aplicación de mapas con `geo:lat,lng`.")
    ]
    waypoints: Annotated[list[RoutePoint], Field(description="Puntos intermedios, en orden.")]
    source: Literal["ORGANIZER"]
    published_at: Annotated[datetime, Field(description="Instante en UTC con sufijo `Z`.")]
    author_name: Annotated[
        str | None,
        Field(description="Nombre del organizador; `null` si Usuarios no respondió."),
    ]


class SuggestionSource(BaseModel):
    source: Literal["users"]
    status: Literal["OK", "UNAVAILABLE"]


class RouteSuggestionsResponse(BaseModel):
    items: list[RouteSuggestionItem]
    personalized: bool = Field(
        description="`false` sin deportes registrados o si Usuarios no respondió: últimas "
        "rutas publicadas (como mucho 20), sin motivos."
    )
    missing_context: list[MissingContext] = Field(
        description="Datos del perfil que faltan para personalizar (`sports` de HU006, `city` "
        "de HU005); ambos si Usuarios no respondió."
    )
    generated_at: Annotated[datetime, Field(description="Instante en UTC con sufijo `Z`.")]
    sources: list[SuggestionSource]


def _context_from(document: dict[str, Any] | None) -> AthleteContext:
    if document is None:
        # Sin cuenta activa resoluble: sin contexto, igual que un perfil vacío.
        return AthleteContext(sports=[], city=None, country_code=None)
    return AthleteContext.model_validate(document)


def _missing(context: AthleteContext | None) -> list[MissingContext]:
    if context is None:
        return ["sports", "city"]
    missing: list[MissingContext] = []
    if not context.sports:
        missing.append("sports")
    if context.city is None:
        missing.append("city")
    return missing


def _item(route: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    return {
        "reference": {"type": "route", "id": route["route_id"]},
        "title": route["name"],
        "summary": route["description"],
        "reasons": route["reasons"],
        **{
            key: route[key]
            for key in (
                "route_id",
                "sport",
                "distance_km",
                "difficulty",
                "city",
                "country_code",
                "start_point",
                "waypoints",
                "source",
                "published_at",
            )
        },
        "author_name": names.get(route["organizer_id"]),
    }


def _athlete(
    verify_token: Callable[[str], VerifiedAccessToken], authorization: str | None
) -> tuple[UUID, str] | Response:
    """Token válido de un `athlete`, o la respuesta de error del BFF."""
    if not authorization or not authorization.lower().startswith(BEARER_PREFIX):
        return _unauthenticated()
    token = authorization[len(BEARER_PREFIX) :].strip()
    try:
        identity = verify_token(token)
        user_id = UUID(identity.subject)
    except (InvalidAccessToken, ValueError):
        return _unauthenticated()
    except JwksUnavailable:
        return _failure(UpstreamUnavailable())
    if identity.actor_type != ATHLETE:
        return error_response(
            HTTPStatus.FORBIDDEN,
            "permission_required",
            "Su tipo de usuario no puede realizar esta operación.",
        )
    return user_id, token


def _capability_denied(
    athlete_snapshot: Callable[[UUID], dict[str, Any]],
    user_id: UUID,
    capability: str = ROUTES_CAPABILITY,
) -> Response | None:
    """Capacidad vigente en Afiliación (`training.routes.suggest` por defecto); `None` si la
    tiene."""
    try:
        snapshot = athlete_snapshot(user_id)
    except (UpstreamTimeout, UpstreamUnavailable) as error:
        return _failure(error)
    granted = snapshot.get("capabilities") or []
    if snapshot.get("subscription_status") == "ACTIVE" and capability in granted:
        return None
    return error_response(
        HTTPStatus.FORBIDDEN, "capability_required", "Su plan no incluye esta funcionalidad."
    )


def _athlete_context(
    users: UsersManagementClient, contexts: AthleteContextCache, user_id: UUID
) -> AthleteContext | None:
    """Contexto vigente del deportista, o `None` si Usuarios no lo entregó."""
    cached = contexts.get(user_id)
    if cached is not None:
        return cached
    try:
        found = users.athlete_context(user_id, timeout=CONTEXT_TIMEOUT_SECONDS)
        context = _context_from(found)
    except (UpstreamTimeout, UpstreamUnavailable, ValueError):
        return None
    if found is not None:
        contexts.put(user_id, context)
    return context


def _author_names(
    users: UsersManagementClient, items: list[dict[str, Any]], author_key: str = "organizer_id"
) -> dict[str, str] | None:
    """Nombre de cada autor de la página (organizador o profesional); `None` si Usuarios no
    respondió."""
    organizers = list(dict.fromkeys(UUID(item[author_key]) for item in items))
    if not organizers:
        return {}
    try:
        found = users.directory_lookup(organizers)
    except (UpstreamTimeout, UpstreamUnavailable, KeyError, ValueError):
        return None
    return {str(item["user_id"]): str(item["full_name"]) for item in found}


def _training_params(context: AthleteContext | None, limit: str | None) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = []
    if context is not None:
        params += [("sport", sport) for sport in context.sports]
        params += [
            (name, value)
            for name, value in (("city", context.city), ("country_code", context.country_code))
            if value is not None
        ]
    if limit is not None:
        params.append(("limit", limit))
    return params


def build_route_suggestions_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    athlete_snapshot: Callable[[UUID], dict[str, Any]],
    users: UsersManagementClient,
    training: TrainingManagementClient,
    cache: AthleteContextCache | None = None,
) -> APIRouter:
    router = APIRouter(tags=["suggestions"])
    contexts = cache or AthleteContextCache()

    @router.get(
        "/v1/me/suggestions/routes",
        response_model=RouteSuggestionsResponse,
        summary="Rutas sugeridas (HU026)",
        description="Rutas publicadas por organizadores para los deportes del deportista: "
        "primero las de su ciudad, luego por distancia, con `reasons`. Sin deportes "
        "registrados o sin respuesta de Usuarios: las últimas publicadas, "
        "`personalized: false` y `missing_context`. Una ruta retirada no se sugiere. Una "
        "sugerencia no garantiza la seguridad de la ruta.",
        responses={
            HTTPStatus.UNAUTHORIZED.value: {
                "model": ErrorResponse,
                "description": "`invalid_access_token`: falta el token o no es válido.",
            },
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` (no es deportista) o `capability_required` "
                f"(`{ROUTES_CAPABILITY}`).",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `query.limit` (`out_of_range`, también si "
                "no es un entero).",
            },
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": "`upstream_unavailable`: plan no verificable o Entrenamientos "
                "caído (si solo falla Usuarios, la lista se entrega sin personalizar).",
            },
            HTTPStatus.GATEWAY_TIMEOUT.value: {
                "model": ErrorResponse,
                "description": "`upstream_timeout`: plan o Entrenamientos sin respuesta.",
            },
        },
    )
    def route_suggestions(
        *,
        limit: Annotated[
            str | None,
            Query(description=f"Entero de 1 a {MAX_LIMIT}; {DEFAULT_LIMIT} por defecto."),
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        caller = _athlete(verify_token, authorization)
        if not isinstance(caller, Response):
            user_id, token = caller
            caller = _capability_denied(athlete_snapshot, user_id) or caller
        if isinstance(caller, Response):
            caller.headers["Cache-Control"] = "no-store"
            return caller
        context = _athlete_context(users, contexts, user_id)
        try:
            upstream = training.route_suggestions(token, _training_params(context, limit))
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            response = _failure(error)
        else:
            response = _relay(upstream, context, lambda routes: _author_names(users, routes))
        response.headers["Cache-Control"] = "no-store"
        return response

    return router


def _relay(
    upstream: Any,
    context: AthleteContext | None,
    author_names: Callable[[list[dict[str, Any]]], dict[str, str] | None],
) -> Response:
    if upstream.status_code != HTTPStatus.OK:
        return _passthrough(
            upstream,
            {
                HTTPStatus.FORBIDDEN,
                HTTPStatus.UNPROCESSABLE_ENTITY,
                HTTPStatus.SERVICE_UNAVAILABLE,
                HTTPStatus.GATEWAY_TIMEOUT,
            },
        )
    try:
        return _compose(upstream.json(), context, author_names)
    except (KeyError, TypeError, ValueError):
        # Respuesta de Entrenamientos fuera de su contrato.
        return _failure(UpstreamUnavailable())


def _compose(
    body: dict[str, Any],
    context: AthleteContext | None,
    author_names: Callable[[list[dict[str, Any]]], dict[str, str] | None],
) -> Response:
    routes: list[dict[str, Any]] = body["items"]
    names = author_names(routes)
    users_ok = context is not None and names is not None
    document = RouteSuggestionsResponse.model_validate(
        {
            "items": [_item(route, names or {}) for route in routes],
            "personalized": bool(body["personalized"]),
            "missing_context": _missing(context),
            "generated_at": datetime.now(UTC).replace(microsecond=0),
            "sources": [{"source": "users", "status": "OK" if users_ok else "UNAVAILABLE"}],
        }
    )
    return JSONResponse(content=document.model_dump(mode="json"))
