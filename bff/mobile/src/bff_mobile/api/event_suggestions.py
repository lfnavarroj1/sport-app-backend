"""HU027 en el canal móvil: eventos sugeridos para el deportista (ADR-041).

1. Borde: token (JWKS), actor `athlete` y capacidad vigente `events.suggest` (Afiliación),
   como el resto de las opciones del plan.
2. Fuentes, en paralelo:
   - Eventos (obligatoria): `GET /v1/events` de ahora a +90 días (ADR-031), recorriendo sus
     páginas de 200. El servicio vuelve a verificar al deportista. Si falla, `503`/`504`
     (HA011).
   - Contexto: `GET /v1/users/{user_id}/athlete-context` (contrato interno) con timeout de
     300 ms y la caché de 5 minutos compartida con HU026/HU028. Sin deportes, sin ciudad o sin
     respuesta de Usuarios la lista es **no personalizada** con `missing_context`; nunca «para
     ti».
3. Reglas en `event_suggestion_rules` (alias `discipline-aliases.v1.json`, ciudad normalizada,
   orden por coincidencia y fecha).
4. Detalle de los eventos devueltos (≤ `limit`): `GET /v1/events/{event_id}` (ADR-032), en
   paralelo, porque `GET /v1/events` no expone `capacity` ni la inscripción propia. Aporta
   `description`, `capacity`, `fee` (ADR-040) y `my_registration` (HU041): un evento con
   inscripción se entrega marcado para no sugerir inscribirse de nuevo. Un evento que dejó de
   estar publicado entre ambas lecturas (`404`) se omite; cualquier otro fallo es `503`/`504`.
Sugerir no inscribe ni verifica cupo (lo decide HU041). Sin persistencia ni `organizer_id`.
"""

import contextvars
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bff_mobile.api.event_registrations import EventFee, Location, MyRegistration
from bff_mobile.api.event_suggestion_rules import (
    AthleteMatch,
    Suggestion,
    athlete_match,
    load_aliases,
    suggest,
)
from bff_mobile.api.route_suggestions import (
    AthleteContext,
    AthleteContextCache,
    MissingContext,
    SuggestionSource,
    _athlete,
    _athlete_context,
    _capability_denied,
    _missing,
)
from bff_mobile.api.service_suggestions import DEFAULT_LIMIT, MAX_LIMIT, _parse_limit, _z
from bff_mobile.api.training_metrics import _failure
from bff_mobile.clients.events_management import EventsManagementClient
from bff_mobile.clients.users_management import (
    UpstreamTimeout,
    UpstreamUnavailable,
    UsersManagementClient,
)
from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from sportapp_platform.tokens import VerifiedAccessToken

EVENTS_CAPABILITY = "events.suggest"
WINDOW = timedelta(days=90)
EVENTS_PAGE_SIZE = 200  # máximo de `GET /v1/events`
MAX_EVENT_PAGES = 10  # 2.000 eventos; HA010 mide 1.000
DETAIL_WORKERS = 10

Reason = Literal["SPORT_MATCH", "CITY_MATCH", "UPCOMING"]

_logger = logging.getLogger("sportapp.bff")


class SuggestedEventReference(BaseModel):
    type: Literal["event"]
    id: UUID


class EventSuggestionItem(BaseModel):
    reference: SuggestedEventReference
    title: Annotated[str, Field(description="Nombre del evento.")]
    summary: Annotated[str, Field(description="Descripción del evento.")]
    reasons: list[Reason] = Field(
        description="`SPORT_MATCH`: la disciplina corresponde a un deporte del deportista "
        "(`discipline-aliases.v1`); `CITY_MATCH`: misma ciudad (sin distinguir mayúsculas ni "
        "acentos); `UPCOMING`: próximo evento sin coincidencias, o lista no personalizada."
    )
    event_id: UUID
    discipline: str
    location: Location
    starts_at: Annotated[datetime, Field(description="Instante en UTC con sufijo `Z`.")]
    ends_at: Annotated[datetime, Field(description="Instante en UTC con sufijo `Z`.")]
    time_zone: Annotated[
        str, Field(description="Zona IANA declarada por el organizador; el cliente convierte.")
    ]
    capacity: Annotated[
        int, Field(description="Cupo declarado; no se verifica disponibilidad (HU041).")
    ]
    fee: Annotated[
        EventFee | None, Field(description="Informativa (no es un cobro); `null` sin tarifa.")
    ]
    my_registration: Annotated[
        MyRegistration | None,
        Field(
            description="Inscripción vigente del deportista; si existe, no se sugiere "
            "inscribirse de nuevo."
        ),
    ]


class EventSuggestionsResponse(BaseModel):
    items: list[EventSuggestionItem]
    personalized: bool = Field(
        description="`false` sin deportes, sin ciudad o si Usuarios no respondió: próximos "
        "eventos publicados por fecha, con `UPCOMING`."
    )
    missing_context: list[MissingContext] = Field(
        description="Datos del perfil que faltan para personalizar (`sports` de HU006, `city` "
        "de HU005); ambos si Usuarios no respondió."
    )
    generated_at: Annotated[datetime, Field(description="Instante en UTC con sufijo `Z`.")]
    sources: list[SuggestionSource]


class _EventsFailure(Exception):
    def __init__(self, response: Response) -> None:
        super().__init__("eventos")
        self.response = response


def _events_failure(upstream: httpx.Response) -> _EventsFailure:
    if upstream.status_code in {
        HTTPStatus.FORBIDDEN,
        HTTPStatus.SERVICE_UNAVAILABLE,
        HTTPStatus.GATEWAY_TIMEOUT,
    }:
        try:
            return _EventsFailure(
                JSONResponse(status_code=upstream.status_code, content=upstream.json())
            )
        except ValueError:
            pass
    _logger.warning(
        "bff.upstream_unexpected_status",
        extra={"fields": {"upstream": "events-management", "status": upstream.status_code}},
    )
    return _EventsFailure(_failure(UpstreamUnavailable()))


def _published_events(
    events: EventsManagementClient, token: str, now: datetime
) -> list[dict[str, Any]]:
    """Eventos publicados de la ventana; `_EventsFailure` si Eventos no responde bien."""
    base = {"from": _z(now), "to": _z(now + WINDOW), "limit": str(EVENTS_PAGE_SIZE)}
    found: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(MAX_EVENT_PAGES):
        params = {**base, "cursor": cursor} if cursor is not None else base
        try:
            upstream = events.published_events_page(token, params)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            raise _EventsFailure(_failure(error)) from None
        if upstream.status_code != HTTPStatus.OK:
            raise _events_failure(upstream)
        try:
            body = upstream.json()
            found += body["items"]
            cursor = body["next_cursor"]
        except (KeyError, TypeError, ValueError):
            raise _EventsFailure(_failure(UpstreamUnavailable())) from None
        if cursor is None:
            return found
    _logger.warning(
        "bff.suggestions_truncated",
        extra={"fields": {"upstream": "events-management", "pages": MAX_EVENT_PAGES}},
    )
    return found


def _detail(events: EventsManagementClient, token: str, event_id: UUID) -> dict[str, Any] | None:
    """Detalle del evento con la inscripción propia; `None` si ya no está publicado."""
    try:
        upstream = events.event_detail(token, event_id)
    except (UpstreamTimeout, UpstreamUnavailable) as error:
        raise _EventsFailure(_failure(error)) from None
    if upstream.status_code == HTTPStatus.NOT_FOUND:
        return None
    if upstream.status_code != HTTPStatus.OK:
        raise _events_failure(upstream)
    try:
        document: dict[str, Any] = upstream.json()
    except ValueError:
        raise _EventsFailure(_failure(UpstreamUnavailable())) from None
    return document


def _details(
    events: EventsManagementClient, token: str, suggestions: list[Suggestion]
) -> list[dict[str, Any] | None]:
    if not suggestions:
        return []
    with ThreadPoolExecutor(max_workers=min(DETAIL_WORKERS, len(suggestions))) as pool:
        # Cada hilo corre con una copia del contexto: conserva la correlación.
        futures = [
            pool.submit(
                contextvars.copy_context().run,
                _detail,
                events,
                token,
                UUID(str(item.event["event_id"])),
            )
            for item in suggestions
        ]
        return [future.result() for future in futures]


def _match(
    context: AthleteContext | None, aliases: dict[str, frozenset[str]]
) -> AthleteMatch | None:
    if context is None or not context.sports or context.city is None:
        return None
    return athlete_match(context.sports, context.city, aliases)


def _item(suggestion: Suggestion, detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "reference": {"type": "event", "id": detail["event_id"]},
        "title": detail["name"],
        "summary": detail["description"],
        "reasons": list(suggestion.reasons),
        **{
            key: detail[key]
            for key in (
                "event_id",
                "discipline",
                "location",
                "starts_at",
                "ends_at",
                "time_zone",
                "capacity",
                "fee",
                "my_registration",
            )
        },
    }


def build_event_suggestions_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    athlete_snapshot: Callable[[UUID], dict[str, Any]],
    users: UsersManagementClient,
    events: EventsManagementClient,
    cache: AthleteContextCache | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(tags=["suggestions"])
    contexts = cache or AthleteContextCache()
    aliases = load_aliases()

    @router.get(
        "/v1/me/suggestions/events",
        response_model=EventSuggestionsResponse,
        summary="Eventos sugeridos (HU027)",
        description="Eventos publicados que empiezan en los próximos 90 días: primero los que "
        "coinciden en deporte y ciudad, luego solo deporte, luego solo ciudad y al final los "
        "demás, cada grupo por `starts_at`, con `reasons`. Sin deportes, sin ciudad o sin "
        "respuesta de Usuarios: próximos eventos por fecha, `personalized: false` y "
        "`missing_context`. Cada ítem trae cupo declarado, tarifa informativa y la "
        "inscripción propia (`my_registration`). Sugerir no inscribe ni verifica cupo.",
        responses={
            HTTPStatus.UNAUTHORIZED.value: {
                "model": ErrorResponse,
                "description": "`invalid_access_token`: falta el token o no es válido.",
            },
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` (no es deportista) o `capability_required` "
                f"(`{EVENTS_CAPABILITY}`, o `calendar.sport.view` que exige Eventos).",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `query.limit` (`out_of_range`, también si "
                "no es un entero).",
            },
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": "`upstream_unavailable`: plan no verificable o Eventos caído (si "
                "solo falla Usuarios, la lista se entrega sin personalizar).",
            },
            HTTPStatus.GATEWAY_TIMEOUT.value: {
                "model": ErrorResponse,
                "description": "`upstream_timeout`: plan o Eventos sin respuesta.",
            },
        },
    )
    def event_suggestions(
        *,
        limit: Annotated[
            str | None,
            Query(description=f"Entero de 1 a {MAX_LIMIT}; {DEFAULT_LIMIT} por defecto."),
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        response = _suggestions(limit, authorization)
        response.headers["Cache-Control"] = "no-store"
        return response

    def _suggestions(limit: str | None, authorization: str | None) -> Response:
        caller = _athlete(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        user_id, token = caller
        denied = _capability_denied(athlete_snapshot, user_id, EVENTS_CAPABILITY)
        if denied is not None:
            return denied
        size = _parse_limit(limit)
        if size is None:
            return error_response(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "validation_error",
                "La solicitud no es válida.",
                [ErrorDetail(field="query.limit", issue="out_of_range")],
            )
        try:
            return _compose(user_id, token, size)
        except _EventsFailure as failure:
            return failure.response
        except (KeyError, TypeError, ValueError):
            # Respuesta de Eventos fuera de su contrato.
            return _failure(UpstreamUnavailable())

    def _compose(user_id: UUID, token: str, size: int) -> Response:
        now = clock().astimezone(UTC).replace(microsecond=0)
        with ThreadPoolExecutor(max_workers=2) as pool:
            # Cada hilo corre con una copia del contexto: conserva la correlación.
            catalog = pool.submit(
                contextvars.copy_context().run, _published_events, events, token, now
            )
            profile = pool.submit(
                contextvars.copy_context().run, _athlete_context, users, contexts, user_id
            )
            context = profile.result()
            published = catalog.result()
        match = _match(context, aliases)
        suggestions = suggest(published, match, now)[:size]
        details = _details(events, token, suggestions)
        document = EventSuggestionsResponse.model_validate(
            {
                "items": [
                    _item(item, detail)
                    for item, detail in zip(suggestions, details, strict=True)
                    if detail is not None
                ],
                "personalized": match is not None,
                "missing_context": _missing(context),
                "generated_at": now,
                "sources": [
                    {"source": "users", "status": "OK" if context is not None else "UNAVAILABLE"}
                ],
            }
        )
        return JSONResponse(content=document.model_dump(mode="json"))

    return router
