"""HU029 en el canal móvil: servicios de acompañamiento y logística sugeridos (ADR-041).

1. Borde: token (JWKS), actor `athlete` y capacidad vigente `services.support.suggest`
   (Afiliación), como el resto de las opciones del plan.
2. Fuentes, en paralelo y cada una con su timeout:
   - Servicios (obligatoria): `GET /v1/services/availability` de ahora a +31 días, solo
     disponibles y solo `COACHING`, `TRANSPORT` y `ACCOMPANIMENT` (ADR-038), recorriendo sus
     páginas. `SPORTS_MEDICINE` nunca se pide. Si falla, `503`/`504` (HA011).
   - Eventos (opcional): `GET /v1/me/event-registrations?status=REGISTERED` (ADR-032); cada
     inscripción ya trae inicio, fin y zona del evento, así que no se pide el detalle. Si falla,
     la lista se entrega sin el motivo logístico y `sources` lo declara.
3. Reglas en `service_suggestion_rules`. Sin inscripciones vigentes (o sin Eventos) la
   respuesta es no personalizada con `missing_context: ["event_registrations"]`; sin
   coincidencias, la lista queda vacía: nunca se sugieren servicios arbitrarios.
4. Proveedor: `author_name` por `POST /v1/users/directory-lookup` (ADR-039); sin respuesta,
   `null` y la fuente `users` en `UNAVAILABLE`.
Sugerir no contrata, reserva ni paga: la tarifa es informativa. Sin persistencia ni datos de
salud.
"""

import contextvars
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bff_mobile.api.route_suggestions import _athlete, _capability_denied
from bff_mobile.api.service_suggestion_rules import (
    SUGGESTED_CATEGORIES,
    WINDOW,
    EventDays,
    Suggestion,
    event_days,
    instant,
    suggest,
)
from bff_mobile.api.training_metrics import _failure
from bff_mobile.clients.complementary_services import ComplementaryServicesClient
from bff_mobile.clients.events_management import EventsManagementClient
from bff_mobile.clients.users_management import (
    UpstreamTimeout,
    UpstreamUnavailable,
    UsersManagementClient,
)
from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from sportapp_platform.tokens import VerifiedAccessToken

SERVICES_CAPABILITY = "services.support.suggest"
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
SERVICES_PAGE_SIZE = 50  # máximo de `GET /v1/services/availability`
MAX_SERVICE_PAGES = 40  # 2.000 servicios; HA010 mide 500
REGISTRATIONS_PAGE_SIZE = 100  # máximo de `GET /v1/me/event-registrations`
MAX_REGISTRATION_PAGES = 10
EVENTS_TIMEOUT_SECONDS = 1.0
DIRECTORY_TIMEOUT_SECONDS = 0.3

Category = Literal["COACHING", "TRANSPORT", "ACCOMPANIMENT"]
Reason = Literal["EVENT_LOGISTICS", "UPCOMING_AVAILABILITY"]

_logger = logging.getLogger("sportapp.bff")


class ServiceReference(BaseModel):
    type: Literal["service"]
    id: UUID


class ServiceFee(BaseModel):
    """Tarifa informativa del servicio (ADR-027): no es un cobro ni una contratación."""

    amount: Annotated[str, Field(description="Cadena decimal en COP, máximo dos decimales.")]
    currency: Literal["COP"]
    billing_unit: Literal["SERVICE", "SESSION", "HOUR", "DAY"]


class NextInterval(BaseModel):
    starts_at: Annotated[datetime, Field(description="Instante en UTC con sufijo `Z`.")]
    ends_at: Annotated[datetime, Field(description="Instante en UTC con sufijo `Z` (exclusivo).")]
    time_zone: Annotated[
        str, Field(description="Zona IANA declarada por el profesional; el cliente convierte.")
    ]


class ServiceSuggestionItem(BaseModel):
    reference: ServiceReference
    title: Annotated[str, Field(description="Nombre del servicio.")]
    summary: Annotated[str, Field(description="Descripción del servicio.")]
    reasons: list[Reason] = Field(
        description="`EVENT_LOGISTICS`: transporte o acompañamiento disponible el día de un "
        "evento inscrito (en la zona del evento); `UPCOMING_AVAILABILITY`: entrenamiento con "
        "disponibilidad en los próximos 31 días."
    )
    service_id: UUID
    category: Category
    modality: Literal["IN_PERSON", "VIRTUAL", "HYBRID"]
    fee: ServiceFee
    next_interval: Annotated[
        NextInterval,
        Field(
            description="Intervalo que justifica el motivo: el primero que coincide con un día "
            "de evento, o el primero disponible."
        ),
    ]
    author_name: Annotated[
        str | None,
        Field(description="Nombre del profesional; `null` si Usuarios no respondió."),
    ]


class ServiceSuggestionSource(BaseModel):
    source: Literal["events", "users"]
    status: Literal["OK", "UNAVAILABLE"]


class ServiceSuggestionsResponse(BaseModel):
    items: list[ServiceSuggestionItem]
    personalized: bool = Field(
        description="`true` si se evaluaron inscripciones vigentes del deportista (eventos de "
        "los próximos 31 días)."
    )
    missing_context: list[Literal["event_registrations"]] = Field(
        description="`event_registrations` sin inscripciones vigentes o si Eventos no "
        "respondió: solo se sugiere entrenamiento con disponibilidad próxima. Una lista vacía "
        "sin `missing_context` significa que no hay servicios que coincidan."
    )
    generated_at: Annotated[datetime, Field(description="Instante en UTC con sufijo `Z`.")]
    sources: list[ServiceSuggestionSource] = Field(
        description="Fuentes opcionales: `events` (motivo logístico) y `users` (nombre del "
        "profesional)."
    )


class _ServicesFailure(Exception):
    def __init__(self, response: Response) -> None:
        super().__init__("servicios")
        self.response = response


def _parse_limit(limit: str | None) -> int | None:
    if limit is None:
        return DEFAULT_LIMIT
    try:
        value = int(limit)
    except ValueError:
        return None
    return value if 1 <= value <= MAX_LIMIT else None


def _z(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _services_failure(upstream: httpx.Response) -> Response:
    if upstream.status_code in {
        HTTPStatus.FORBIDDEN,
        HTTPStatus.SERVICE_UNAVAILABLE,
        HTTPStatus.GATEWAY_TIMEOUT,
    }:
        try:
            return JSONResponse(status_code=upstream.status_code, content=upstream.json())
        except ValueError:
            pass
    _logger.warning(
        "bff.upstream_unexpected_status",
        extra={"fields": {"upstream": "complementary-services", "status": upstream.status_code}},
    )
    return _failure(UpstreamUnavailable())


def _published_services(
    services: ComplementaryServicesClient, token: str, now: datetime
) -> list[dict[str, Any]]:
    """Todas las páginas de la ventana; `_ServicesFailure` si Servicios no responde bien."""
    base = [
        ("from", _z(now)),
        ("to", _z(now + WINDOW)),
        *(("category", category) for category in SUGGESTED_CATEGORIES),
        ("availability", "available"),
        ("limit", str(SERVICES_PAGE_SIZE)),
    ]
    found: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(MAX_SERVICE_PAGES):
        params = base + ([("cursor", cursor)] if cursor is not None else [])
        try:
            upstream = services.service_availability(token, params)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            raise _ServicesFailure(_failure(error)) from None
        if upstream.status_code != HTTPStatus.OK:
            raise _ServicesFailure(_services_failure(upstream))
        try:
            body = upstream.json()
            found += body["items"]
            cursor = body["next_cursor"]
        except (KeyError, TypeError, ValueError):
            raise _ServicesFailure(_failure(UpstreamUnavailable())) from None
        if cursor is None:
            return found
    _logger.warning(
        "bff.suggestions_truncated",
        extra={"fields": {"upstream": "complementary-services", "pages": MAX_SERVICE_PAGES}},
    )
    return found


def _registered_event_days(
    events: EventsManagementClient, token: str, now: datetime
) -> list[EventDays] | None:
    """Días de los eventos inscritos que aún no terminan y empiezan en la ventana; `None` si
    Eventos no respondió."""
    window_end = now + WINDOW
    days: list[EventDays] = []
    cursor: str | None = None
    try:
        for _ in range(MAX_REGISTRATION_PAGES):
            params = {"status": "REGISTERED", "limit": str(REGISTRATIONS_PAGE_SIZE)}
            if cursor is not None:
                params["cursor"] = cursor
            upstream = events.my_registrations(token, params, timeout=EVENTS_TIMEOUT_SECONDS)
            if upstream.status_code != HTTPStatus.OK:
                _logger.warning(
                    "bff.upstream_unexpected_status",
                    extra={
                        "fields": {"upstream": "events-management", "status": upstream.status_code}
                    },
                )
                return None
            body = upstream.json()
            last_start: datetime | None = None
            for registration in body["items"]:
                event = registration["event"]
                last_start, ends_at = instant(event["starts_at"]), instant(event["ends_at"])
                if ends_at > now and last_start < window_end:
                    days.append(event_days(last_start, ends_at, event["time_zone"]))
            cursor = body["next_cursor"]
            # Ordenadas por inicio del evento: las páginas siguientes quedan fuera de la ventana.
            if cursor is None or (last_start is not None and last_start >= window_end):
                return days
    except (UpstreamTimeout, UpstreamUnavailable, KeyError, TypeError, ValueError):
        return None
    return days


def _provider_names(
    users: UsersManagementClient, suggestions: list[Suggestion]
) -> dict[str, str] | None:
    providers = list(dict.fromkeys(UUID(item.service["provider_id"]) for item in suggestions))
    if not providers:
        return {}
    try:
        found = users.directory_lookup(providers, timeout=DIRECTORY_TIMEOUT_SECONDS)
        return {str(item["user_id"]): str(item["full_name"]) for item in found}
    except (UpstreamTimeout, UpstreamUnavailable, KeyError, TypeError, ValueError):
        return None


def _item(suggestion: Suggestion, names: dict[str, str]) -> dict[str, Any]:
    service = suggestion.service
    return {
        "reference": {"type": "service", "id": service["service_id"]},
        "title": service["name"],
        "summary": service["description"],
        "reasons": [suggestion.reason],
        "service_id": service["service_id"],
        "category": service["category"],
        "modality": service["modality"],
        "fee": service["price"],
        "next_interval": {
            "starts_at": suggestion.interval.starts_at,
            "ends_at": suggestion.interval.ends_at,
            "time_zone": suggestion.interval.time_zone,
        },
        "author_name": names.get(str(service["provider_id"])),
    }


def build_service_suggestions_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    athlete_snapshot: Callable[[UUID], dict[str, Any]],
    users: UsersManagementClient,
    events: EventsManagementClient,
    services: ComplementaryServicesClient,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(tags=["suggestions"])

    @router.get(
        "/v1/me/suggestions/services",
        response_model=ServiceSuggestionsResponse,
        summary="Servicios de acompañamiento y logística sugeridos (HU029)",
        description="Servicios publicados con disponibilidad en los próximos 31 días: "
        "transporte y acompañamiento disponibles el día de un evento inscrito "
        "(`EVENT_LOGISTICS`, primero) y entrenamiento con disponibilidad próxima "
        "(`UPCOMING_AVAILABILITY`), por inicio del intervalo. Nunca medicina deportiva. Sin "
        "inscripciones vigentes: `personalized: false` y `missing_context`. Si Eventos no "
        "responde, la lista llega sin el motivo logístico y `sources` lo declara. Sugerir no "
        "contrata ni reserva: la tarifa es informativa.",
        responses={
            HTTPStatus.UNAUTHORIZED.value: {
                "model": ErrorResponse,
                "description": "`invalid_access_token`: falta el token o no es válido.",
            },
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` (no es deportista) o `capability_required` "
                f"(`{SERVICES_CAPABILITY}`).",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `query.limit` (`out_of_range`, también si "
                "no es un entero).",
            },
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": "`upstream_unavailable`: plan no verificable o Servicios caído "
                "(si solo falla Eventos o Usuarios, la lista se entrega degradada).",
            },
            HTTPStatus.GATEWAY_TIMEOUT.value: {
                "model": ErrorResponse,
                "description": "`upstream_timeout`: plan o Servicios sin respuesta.",
            },
        },
    )
    def service_suggestions(
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
        denied = _capability_denied(athlete_snapshot, user_id, SERVICES_CAPABILITY)
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
        now = clock().astimezone(UTC).replace(microsecond=0)
        with ThreadPoolExecutor(max_workers=2) as pool:
            # Cada hilo corre con una copia del contexto: conserva la correlación.
            catalog = pool.submit(
                contextvars.copy_context().run, _published_services, services, token, now
            )
            registered = pool.submit(
                contextvars.copy_context().run, _registered_event_days, events, token, now
            )
            try:
                published = catalog.result()
            except _ServicesFailure as failure:
                return failure.response
            days = registered.result()
        try:
            suggestions = suggest(published, days or [])[:size]
            names = _provider_names(users, suggestions)
            personalized = bool(days)
            document = ServiceSuggestionsResponse.model_validate(
                {
                    "items": [_item(item, names or {}) for item in suggestions],
                    "personalized": personalized,
                    "missing_context": [] if personalized else ["event_registrations"],
                    "generated_at": now,
                    "sources": [
                        {"source": "events", "status": "OK" if days is not None else "UNAVAILABLE"},
                        {"source": "users", "status": "OK" if names is not None else "UNAVAILABLE"},
                    ],
                }
            )
        except (KeyError, TypeError, ValueError):
            # Respuesta de Servicios fuera de su contrato.
            return _failure(UpstreamUnavailable())
        return JSONResponse(content=document.model_dump(mode="json"))

    return router
