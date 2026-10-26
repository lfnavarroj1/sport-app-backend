"""HU018: `GET /v1/me/integrated-calendar` en el canal web (ADR-039 con el patrón de ADR-031).

El BFF autentica (JWKS) y autoriza por actor: `organizer` y `service_provider` con el permiso
`calendar.integrated.view`; `athlete` con la capacidad vigente `calendar.sport.view`
(Afiliación). Después consulta en paralelo, cada una con su propio plazo, solo consultas que
ya existen en los dominios, y compone el resultado:

- organizador: `OWN_EVENT` (`GET /v1/me/events`, `DRAFT` y `PUBLISHED` propios);
- profesional: `OWN_SERVICE_AVAILABILITY` (intervalos de `GET /v1/me/services`) y
  `EVENT_PUBLISHED` (`GET /v1/events`). Eventos todavía no admite al profesional en esa
  consulta (`T-HU018-BE-EVT`): hasta entonces la fuente se declara `UNAVAILABLE`;
- deportista: `EVENT_PUBLISHED`, `TRAINING_PLAN_SESSION` (`GET /v1/me/training-plan-sessions`),
  `ASSOCIATED_SERVICE_AVAILABILITY` (`GET /v1/me/service-associations` + intervalos de
  `GET /v1/services/availability`) y `EVENT_REGISTRATION` (`GET /v1/me/event-registrations`).

Una fuente caída se declara en `sources[]` y el resto se entrega con `200`; si fallan todas,
`503`/`504`. Cada fuente aporta hasta 200 elementos y marca `truncated`. El token del usuario
viaja a cada servicio, que vuelve a verificar identidad, permiso y propiedad: los borradores
ajenos nunca llegan. No persiste nada ni espera a ninguna proyección.
"""

import contextlib
import contextvars
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bff_web.api.integrated_calendar_composition import (
    ASSOCIATED_SERVICE_AVAILABILITY,
    EVENT_PUBLISHED,
    EVENT_REGISTRATION,
    EVENT_STATUSES,
    OWN_APPOINTMENT,
    OWN_EVENT,
    OWN_SERVICE_AVAILABILITY,
    SERVICE_STATUSES,
    SOURCE_LIMIT,
    TRAINING_PLAN_SESSION,
    InvalidPeriod,
    Period,
    associated_service_item,
    availability_windows,
    capped,
    event_overlaps,
    instant,
    interval_overlaps,
    normalize_period,
    ordered,
    own_appointment_item,
    own_event_item,
    own_service_item,
    published_event_item,
    registration_item,
    session_item,
    utc_instant,
)
from bff_web.api.training import (
    _bearer_token,
    _permission_required,
    _unauthenticated,
    _upstream_failure,
)
from bff_web.clients.complementary_services import ComplementaryServicesClient
from bff_web.clients.events_management import EventsManagementClient
from bff_web.clients.training_management import TrainingManagementClient
from bff_web.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.authorization import ActorPermissions
from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from sportapp_platform.tokens import InvalidAccessToken, JwksUnavailable, VerifiedAccessToken

ATHLETE = "athlete"
ORGANIZER = "organizer"
SERVICE_PROVIDER = "service_provider"
INTEGRATED_PERMISSION = "calendar.integrated.view"
SPORT_CAPABILITY = "calendar.sport.view"
CAPABILITY_REQUIRED = "capability_required"
# Fuentes de cada actor y su orden en `sources[]` (ADR-039).
SOURCES_BY_ACTOR: dict[str, tuple[str, ...]] = {
    ORGANIZER: (OWN_EVENT,),
    # HU040 agrega `OWN_APPOINTMENT` al final: las fuentes previas conservan su orden.
    SERVICE_PROVIDER: (OWN_SERVICE_AVAILABILITY, EVENT_PUBLISHED, OWN_APPOINTMENT),
    ATHLETE: (
        EVENT_PUBLISHED,
        TRAINING_PLAN_SESSION,
        ASSOCIATED_SERVICE_AVAILABILITY,
        EVENT_REGISTRATION,
    ),
}
# Páginas máximas de cada consulta reutilizada (ADR-033, ADR-038, ADR-032).
PAGE_LIMIT = 100
AVAILABILITY_PAGE_LIMIT = 50
AVAILABILITY_SPAN = timedelta(days=31)
# Una cita dura como su intervalo (≤ 24 h, ADR-027): se piden también las que empezaron
# hasta un día antes del periodo para no perder las que lo cruzan.
APPOINTMENT_SPAN = timedelta(hours=24)
INSTANT = "Instante ISO 8601 con desfase (zona del navegador); sin ambos, el mes UTC en curso."

_logger = logging.getLogger("sportapp.bff")

SourceName = Literal[
    "OWN_EVENT",
    "EVENT_PUBLISHED",
    "EVENT_REGISTRATION",
    "TRAINING_PLAN_SESSION",
    "OWN_SERVICE_AVAILABILITY",
    "ASSOCIATED_SERVICE_AVAILABILITY",
    "OWN_APPOINTMENT",
]
SourceStatus = Literal["OK", "UNAVAILABLE", "TIMEOUT"]


class IntegratedLocation(BaseModel):
    name: str
    city: str
    country_code: str


class IntegratedEventReference(BaseModel):
    event_id: UUID


class IntegratedRegistrationReference(BaseModel):
    event_id: UUID
    registration_id: UUID


class IntegratedSessionReference(BaseModel):
    plan_id: UUID
    session_id: UUID


class IntegratedServiceReference(BaseModel):
    service_id: UUID


class IntegratedAssociatedServiceReference(BaseModel):
    service_id: UUID
    association_id: UUID


class _IntegratedTimedItem(BaseModel):
    item_id: Annotated[str, Field(description="`<source>:<identificador>`, único en la respuesta.")]
    occurs_on: Annotated[date, Field(description="Fecha de inicio en la `time_zone` del elemento.")]
    title: str
    starts_at: Annotated[datetime, Field(description="Instante original en UTC (`Z`).")]
    ends_at: datetime
    time_zone: Annotated[str, Field(description="Zona IANA declarada por el dominio.")]


class IntegratedOwnEventItem(_IntegratedTimedItem):
    source: Literal["OWN_EVENT"]
    status: Literal["DRAFT", "PUBLISHED"]
    sport: str
    location: IntegratedLocation
    reference: IntegratedEventReference


class IntegratedPublishedEventItem(_IntegratedTimedItem):
    source: Literal["EVENT_PUBLISHED"]
    status: Literal["PUBLISHED"]
    sport: str
    location: IntegratedLocation
    reference: IntegratedEventReference


class IntegratedEventRegistrationItem(_IntegratedTimedItem):
    source: Literal["EVENT_REGISTRATION"]
    status: Literal["REGISTERED"]
    location: IntegratedLocation
    reference: IntegratedRegistrationReference


class IntegratedTrainingSessionItem(BaseModel):
    item_id: str
    source: Literal["TRAINING_PLAN_SESSION"]
    status: Literal["ASSIGNED"]
    occurs_on: Annotated[date, Field(description="`scheduled_date` de la sesión (sin hora).")]
    title: str
    starts_at: None
    ends_at: None
    time_zone: None
    sport: str
    duration_minutes: int
    rpe: int
    reference: IntegratedSessionReference


class IntegratedOwnServiceAvailabilityItem(_IntegratedTimedItem):
    source: Literal["OWN_SERVICE_AVAILABILITY"]
    status: Annotated[
        Literal["DRAFT", "PUBLISHED"], Field(description="Estado del servicio propio.")
    ]
    category: str
    modality: str
    capacity: int
    reference: IntegratedServiceReference


class IntegratedAssociatedServiceAvailabilityItem(_IntegratedTimedItem):
    source: Literal["ASSOCIATED_SERVICE_AVAILABILITY"]
    status: Literal["AVAILABLE"]
    category: str
    modality: str
    capacity: int
    reference: IntegratedAssociatedServiceReference


class IntegratedAppointmentReference(BaseModel):
    appointment_id: UUID
    service_id: UUID
    availability_id: UUID


class IntegratedOwnAppointmentItem(_IntegratedTimedItem):
    """HU040 (ADR-046 §4): cita reservada en un servicio propio del profesional. Solo
    `BOOKED`: una cancelada no aparece (ADR-039, CA4); el detalle con el nombre del deportista
    está en `GET /v1/me/appointments`."""

    source: Literal["OWN_APPOINTMENT"]
    status: Literal["BOOKED"]
    category: str
    modality: str
    reference: IntegratedAppointmentReference


IntegratedItem = Annotated[
    IntegratedOwnEventItem
    | IntegratedPublishedEventItem
    | IntegratedEventRegistrationItem
    | IntegratedTrainingSessionItem
    | IntegratedOwnServiceAvailabilityItem
    | IntegratedAssociatedServiceAvailabilityItem
    | IntegratedOwnAppointmentItem,
    Field(discriminator="source"),
]


class IntegratedSource(BaseModel):
    name: SourceName
    status: SourceStatus
    truncated: Annotated[bool, Field(description="La fuente tenía más de 200 elementos.")]


class IntegratedCalendarResponse(BaseModel):
    from_: Annotated[datetime, Field(alias="from")]
    to: datetime
    items: list[IntegratedItem]
    sources: Annotated[
        list[IntegratedSource],
        Field(description="Fuentes del actor; una distinta de `OK` está incompleta."),
    ]
    evaluated_at: datetime


@dataclass(frozen=True)
class SourceResult:
    name: str
    status: SourceStatus
    items: list[dict[str, Any]]
    truncated: bool = False


class _Rejected(Exception):
    """El servicio respondió algo distinto de `200`."""

    def __init__(self, status: int, code: str | None) -> None:
        super().__init__(status)
        self.status = status
        self.code = code


def _page(response: httpx.Response) -> dict[str, Any]:
    if response.status_code != HTTPStatus.OK:
        code = None
        with contextlib.suppress(ValueError, AttributeError):
            code = response.json().get("code")
        raise _Rejected(response.status_code, code)
    document: dict[str, Any] = response.json()
    return document


class _Deadline:
    """Plazo propio de una fuente que necesita varias páginas."""

    def __init__(self, seconds: float, monotonic: Callable[[], float]) -> None:
        self._monotonic = monotonic
        self._until = monotonic() + seconds

    def check(self) -> None:
        if self._monotonic() > self._until:
            raise UpstreamTimeout()


Collected = tuple[list[dict[str, Any]], bool]


def _paged(params: dict[str, str], cursor: str | None) -> dict[str, str]:
    return params if cursor is None else {**params, "cursor": cursor}


def _collect(name: str, fetch: Callable[[], Collected]) -> SourceResult:
    try:
        items, truncated = fetch()
    except UpstreamTimeout:
        return SourceResult(name, "TIMEOUT", [])
    except UpstreamUnavailable:
        return SourceResult(name, "UNAVAILABLE", [])
    except _Rejected as rejected:
        _logger.warning(
            "bff.integrated_calendar_source_status",
            extra={"fields": {"source": name, "status": rejected.status, "code": rejected.code}},
        )
        return SourceResult(name, "UNAVAILABLE", [])
    except (ValueError, KeyError, TypeError):
        _logger.warning(
            "bff.integrated_calendar_source_invalid", extra={"fields": {"source": name}}
        )
        return SourceResult(name, "UNAVAILABLE", [])
    return SourceResult(name, "OK", items, truncated)


def _compose(period: Period, results: list[SourceResult], now: datetime) -> Response:
    if all(result.status != "OK" for result in results):
        if all(result.status == "TIMEOUT" for result in results):
            return _upstream_failure(UpstreamTimeout())
        return _upstream_failure(UpstreamUnavailable())
    body = {
        "from": utc_instant(period.period_from),
        "to": utc_instant(period.period_to),
        "items": ordered([item for result in results for item in result.items]),
        "sources": [
            {"name": result.name, "status": result.status, "truncated": result.truncated}
            for result in results
        ],
        "evaluated_at": utc_instant(now),
    }
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


class _Sources:
    """Lectura de cada fuente con el token del usuario; cada método devuelve sus elementos
    (como máximo 200) y si el periodo tenía más."""

    def __init__(
        self,
        *,
        token: str,
        period: Period,
        events: EventsManagementClient,
        services: ComplementaryServicesClient,
        training: TrainingManagementClient,
        deadline: Callable[[], _Deadline],
    ) -> None:
        self._token = token
        self._period = period
        self._events = events
        self._services = services
        self._training = training
        self._deadline = deadline

    def fetcher(self, name: str) -> Callable[[], Collected]:
        fetchers: dict[str, Callable[[], Collected]] = {
            OWN_EVENT: self.own_events,
            EVENT_PUBLISHED: self.published_events,
            EVENT_REGISTRATION: self.registrations,
            TRAINING_PLAN_SESSION: self.planned_sessions,
            OWN_SERVICE_AVAILABILITY: self.own_services,
            ASSOCIATED_SERVICE_AVAILABILITY: self.associated_services,
            OWN_APPOINTMENT: self.own_appointments,
        }
        return fetchers[name]

    def _time_ordered(
        self,
        call: Callable[[dict[str, str]], httpx.Response],
        starts_at: Callable[[dict[str, Any]], datetime],
        ends_at: Callable[[dict[str, Any]], datetime],
        keep: Callable[[dict[str, Any]], bool],
        to_item: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> Collected:
        """Recorre páginas ordenadas por inicio ascendente y se detiene en el primer elemento
        que empieza después del periodo o al juntar uno más del límite."""
        deadline, cursor = self._deadline(), None
        items: list[dict[str, Any]] = []
        while True:
            page = _page(call(_paged({"limit": str(PAGE_LIMIT)}, cursor)))
            for element in page["items"]:
                start = starts_at(element)
                if start > self._period.period_to:
                    return capped(items)
                if keep(element) and event_overlaps(self._period, start, ends_at(element)):
                    items.append(to_item(element))
                    if len(items) > SOURCE_LIMIT:
                        return capped(items)
            cursor = page["next_cursor"]
            if cursor is None:
                return capped(items)
            deadline.check()

    def own_events(self) -> Collected:
        return self._time_ordered(
            lambda params: self._events.list_my_events(self._token, params),
            lambda event: instant(event["starts_at"]),
            lambda event: instant(event["ends_at"]),
            lambda event: event["status"] in EVENT_STATUSES,
            own_event_item,
        )

    def registrations(self) -> Collected:
        return self._time_ordered(
            lambda params: self._events.list_my_registrations(self._token, params),
            lambda registration: instant(registration["event"]["starts_at"]),
            lambda registration: instant(registration["event"]["ends_at"]),
            lambda registration: registration["status"] == "REGISTERED",
            registration_item,
        )

    def published_events(self) -> Collected:
        params = {
            "from": utc_instant(self._period.period_from),
            "to": utc_instant(self._period.period_to),
            "limit": str(SOURCE_LIMIT),
        }
        page = _page(self._events.list_events(self._token, params))
        items = [published_event_item(e) for e in page["items"] if e["status"] == "PUBLISHED"]
        return items, page["next_cursor"] is not None

    def planned_sessions(self) -> Collected:
        params = {
            "from": self._period.date_from.isoformat(),
            "to": self._period.date_to.isoformat(),
            "limit": str(SOURCE_LIMIT),
        }
        page = _page(self._training.planned_sessions(self._token, params))
        return [session_item(session) for session in page["items"]], page["next_cursor"] is not None

    def _all_pages(self, call: Callable[[str | None], httpx.Response]) -> list[dict[str, Any]]:
        """Todas las páginas de una consulta; `call` recibe el cursor (`None` en la primera)."""
        deadline, cursor = self._deadline(), None
        elements: list[dict[str, Any]] = []
        while True:
            page = _page(call(cursor))
            elements += page["items"]
            cursor = page["next_cursor"]
            if cursor is None:
                return elements
            deadline.check()

    def own_services(self) -> Collected:
        """Servicios propios `DRAFT` y `PUBLISHED`; la consulta ordena por actualización, así
        que se leen todas las páginas antes de recortar."""
        services = self._all_pages(
            lambda cursor: self._services.list_my_services(
                self._token, _paged({"limit": str(PAGE_LIMIT)}, cursor)
            )
        )
        items = [
            own_service_item(service, interval)
            for service in services
            if service["status"] in SERVICE_STATUSES
            for interval in service["availability"]
            if interval_overlaps(
                self._period, instant(interval["starts_at"]), instant(interval["ends_at"])
            )
        ]
        return capped(items)

    def associated_services(self) -> Collected:
        """Asociaciones activas (HU017) cruzadas con el catálogo publicado (HU020), que admite
        hasta 31 días por consulta y solo devuelve intervalos futuros que empiezan en
        `[from, to)`. Sin la capacidad médica el deportista no tiene servicios asociados
        autorizados: la fuente queda vacía, no incompleta (ADR-039, CA4)."""
        try:
            associations = self._all_pages(
                lambda cursor: self._services.list_my_associations(
                    self._token, _paged({"status": "ACTIVE", "limit": str(PAGE_LIMIT)}, cursor)
                )
            )
        except _Rejected as rejected:
            if rejected.status == HTTPStatus.FORBIDDEN and rejected.code == CAPABILITY_REQUIRED:
                return [], False
            raise
        association_of = {
            str(item["service_id"]): str(item["association_id"]) for item in associations
        }
        if not association_of:
            return [], False
        categories = sorted({str(item["service"]["category"]) for item in associations})
        items: dict[str, dict[str, Any]] = {}
        for window_from, window_to in availability_windows(self._period, AVAILABILITY_SPAN):
            query = [
                ("from", utc_instant(window_from)),
                ("to", utc_instant(window_to)),
                ("availability", "available"),
                ("limit", str(AVAILABILITY_PAGE_LIMIT)),
                *(("category", category) for category in categories),
            ]
            for service in self._catalog(query):
                association_id = association_of.get(str(service["service_id"]))
                if association_id is None:
                    continue
                for interval in service["availability"]:
                    item = associated_service_item(service, interval, association_id)
                    items[item["item_id"]] = item
        return capped(list(items.values()))

    def own_appointments(self) -> Collected:
        """HU040: citas `BOOKED` de los servicios propios (`GET /v1/me/appointments`, ordenado
        por inicio)."""
        window = {
            "from": utc_instant(self._period.period_from - APPOINTMENT_SPAN),
            "to": utc_instant(self._period.period_to + timedelta(seconds=1)),
            "status": "BOOKED",
        }
        return self._time_ordered(
            lambda params: self._services.list_my_appointments(self._token, {**window, **params}),
            lambda appointment: instant(appointment["starts_at"]),
            lambda appointment: instant(appointment["ends_at"]),
            lambda appointment: appointment["status"] == "BOOKED",
            own_appointment_item,
        )

    def _catalog(self, query: list[tuple[str, str]]) -> list[dict[str, Any]]:
        return self._all_pages(
            lambda cursor: self._services.service_availability(
                self._token, query + ([("cursor", cursor)] if cursor else [])
            )
        )


def build_integrated_calendar_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    permissions: ActorPermissions,
    athlete_snapshot: Callable[[UUID], dict[str, Any]],
    events: EventsManagementClient,
    services: ComplementaryServicesClient,
    training: TrainingManagementClient,
    channel: str,
    source_timeout: float,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
) -> APIRouter:
    router = APIRouter(prefix="/v1/me", tags=["calendar"])

    def authenticate(authorization: str | None) -> tuple[UUID, str, str] | Response:
        token = _bearer_token(authorization)
        if token is None:
            return _unauthenticated()
        try:
            identity = verify_token(token)
            user_id = UUID(identity.subject)
        except (InvalidAccessToken, ValueError):
            return _unauthenticated()
        except JwksUnavailable:
            _logger.warning("bff.jwks_unavailable")
            return _upstream_failure(UpstreamUnavailable())
        actor = identity.actor_type or ""
        if actor not in SOURCES_BY_ACTOR or (
            actor != ATHLETE and INTEGRATED_PERMISSION not in permissions.for_actor(actor, channel)
        ):
            return _permission_required()
        return user_id, token, actor

    def require_capability(user_id: UUID) -> Response | None:
        try:
            snapshot = athlete_snapshot(user_id)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        except ValueError:
            return _upstream_failure(UpstreamUnavailable())
        granted = snapshot.get("capabilities") or []
        if snapshot.get("subscription_status") == "ACTIVE" and SPORT_CAPABILITY in granted:
            return None
        return error_response(
            HTTPStatus.FORBIDDEN, CAPABILITY_REQUIRED, "Su plan no incluye esta funcionalidad."
        )

    def gather(actor: str, token: str, period: Period) -> list[SourceResult]:
        sources = _Sources(
            token=token,
            period=period,
            events=events,
            services=services,
            training=training,
            deadline=lambda: _Deadline(source_timeout, monotonic),
        )
        names = SOURCES_BY_ACTOR[actor]
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            # Cada hilo corre con una copia del contexto: conserva la correlación.
            futures = [
                pool.submit(contextvars.copy_context().run, _collect, name, sources.fetcher(name))
                for name in names
            ]
            return [future.result() for future in futures]

    @router.get(
        "/integrated-calendar",
        response_model=IntegratedCalendarResponse,
        summary="Calendario integrado de eventos y servicios por actor (HU018)",
        description="Compone por actor las fuentes de ADR-039: organizador `OWN_EVENT`; "
        "profesional `OWN_SERVICE_AVAILABILITY`, `EVENT_PUBLISHED` y `OWN_APPOINTMENT` (citas "
        "`BOOKED` de sus servicios, HU040); deportista "
        "`EVENT_PUBLISHED`, `TRAINING_PLAN_SESSION`, `ASSOCIATED_SERVICE_AVAILABILITY` y "
        "`EVENT_REGISTRATION`. Periodo de hasta 92 días; hasta 200 elementos por fuente "
        "(`truncated`); orden `occurs_on`, `starts_at` (nulos primero), `source`, `item_id`. "
        "Instantes en UTC con la `time_zone` de cada elemento: el cliente los presenta en la "
        "zona del navegador. Una fuente caída se declara en `sources[]` (`UNAVAILABLE` o "
        "`TIMEOUT`) y el calendario no afirma que no haya actividades.",
        responses={
            HTTPStatus.UNAUTHORIZED.value: {
                "model": ErrorResponse,
                "description": "`invalid_access_token`: falta el token o no es válido.",
            },
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` (actor sin `calendar.integrated.view`) o "
                "`capability_required` (deportista sin `calendar.sport.view`).",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` en `query.from`/`query.to`: "
                "`invalid_datetime`, `ends_before_starts`, `out_of_range` (más de 92 días) o "
                "`required` (solo uno de los dos extremos).",
            },
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": "`upstream_unavailable`: capacidad no evaluable o todas las "
                "fuentes caídas.",
            },
            HTTPStatus.GATEWAY_TIMEOUT.value: {
                "model": ErrorResponse,
                "description": "`upstream_timeout`: capacidad o todas las fuentes sin respuesta.",
            },
        },
    )
    def integrated_calendar(
        raw_from: Annotated[str | None, Query(alias="from", description=INSTANT)] = None,
        raw_to: Annotated[str | None, Query(alias="to", description=INSTANT)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        started = time.monotonic()
        caller = authenticate(authorization)
        if isinstance(caller, Response):
            return caller
        user_id, token, actor = caller
        try:
            period = normalize_period(raw_from, raw_to, clock())
        except InvalidPeriod as error:
            return error_response(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                error.code,
                "El periodo no es válido.",
                [ErrorDetail(field=f"query.{error.field}", issue=error.issue)],
            )
        if actor == ATHLETE:
            rejection = require_capability(user_id)
            if rejection is not None:
                return rejection
        results = gather(actor, token, period)
        _logger.info(
            "bff.integrated_calendar",
            extra={
                "fields": {
                    "actor_type": actor,
                    **{f"{result.name.lower()}_status": result.status for result in results},
                    **{f"{result.name.lower()}_items": len(result.items) for result in results},
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                }
            },
        )
        return _compose(period, results, clock())

    return router
