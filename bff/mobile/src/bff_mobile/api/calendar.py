"""HU035: `GET /v1/me/calendar` en el canal móvil (contrato público de ADR-031).

El BFF autentica (JWKS), exige `athlete` y la capacidad vigente `calendar.sport.view`
(Afiliación), y compone dos consultas de lectura en paralelo, cada una con su propio
timeout: eventos publicados (`events_management`) y sesiones planificadas propias
(`training_management`). Si una fuente falla responde `200` con la otra y la declara en
`sources[]`; si fallan ambas, `503`/`504`. Cada fuente aporta hasta 200 elementos y marca
`truncated`. No persiste nada ni devuelve `athlete_id`, `organizer_id` o `professional_id`.
"""

import contextvars
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bff_mobile.api.calendar_composition import (
    InvalidPeriod,
    Period,
    event_item,
    normalize_period,
    ordered,
    session_item,
    utc_instant,
)
from bff_mobile.clients.events_management import EventsManagementClient
from bff_mobile.clients.training_management import TrainingManagementClient
from bff_mobile.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from sportapp_platform.tokens import InvalidAccessToken, JwksUnavailable, VerifiedAccessToken

ATHLETE = "athlete"
BEARER_PREFIX = "bearer "
CALENDAR_CAPABILITY = "calendar.sport.view"
SOURCE_LIMIT = 200
INSTANT = "Instante ISO 8601 con desfase (zona del dispositivo); sin ambos, el mes UTC en curso."

_logger = logging.getLogger("sportapp.bff")

SourceStatus = Literal["OK", "UNAVAILABLE", "TIMEOUT"]


class CalendarLocation(BaseModel):
    name: str
    city: str
    country_code: str


class EventReference(BaseModel):
    event_id: UUID


class SessionReference(BaseModel):
    plan_id: UUID
    session_id: UUID


class EventCalendarItem(BaseModel):
    item_id: str
    source: Literal["EVENT_PUBLISHED"]
    occurs_on: Annotated[date, Field(description="Fecha del evento en su `time_zone`.")]
    title: str
    sport: str
    starts_at: datetime
    ends_at: datetime
    time_zone: str
    location: CalendarLocation
    reference: EventReference


class SessionCalendarItem(BaseModel):
    item_id: str
    source: Literal["TRAINING_PLAN_SESSION"]
    occurs_on: Annotated[date, Field(description="`scheduled_date` de la sesión (sin hora).")]
    title: str
    sport: str
    starts_at: None
    ends_at: None
    time_zone: None
    duration_minutes: int
    rpe: int
    reference: SessionReference


class CalendarSource(BaseModel):
    name: Literal["events", "training"]
    status: SourceStatus
    truncated: bool


class CalendarResponse(BaseModel):
    from_: Annotated[datetime, Field(alias="from")]
    to: datetime
    items: list[Annotated[EventCalendarItem | SessionCalendarItem, Field(discriminator="source")]]
    sources: list[CalendarSource]
    evaluated_at: datetime


@dataclass(frozen=True)
class SourceResult:
    name: str
    status: SourceStatus
    items: list[dict[str, Any]]
    truncated: bool = False


def _unauthenticated() -> Response:
    response = error_response(
        HTTPStatus.UNAUTHORIZED, InvalidAccessToken.code, "La sesión no es válida."
    )
    response.headers["WWW-Authenticate"] = "Bearer"
    return response


def _failure(error: UpstreamTimeout | UpstreamUnavailable) -> Response:
    if isinstance(error, UpstreamTimeout):
        return error_response(
            HTTPStatus.GATEWAY_TIMEOUT, error.code, "Un servicio interno no respondió."
        )
    return error_response(
        HTTPStatus.SERVICE_UNAVAILABLE, error.code, "Un servicio interno no está disponible."
    )


def _fetch(
    name: str,
    call: Callable[[], httpx.Response],
    to_item: Callable[[dict[str, Any]], dict[str, Any]],
) -> SourceResult:
    try:
        response = call()
    except UpstreamTimeout:
        return SourceResult(name, "TIMEOUT", [])
    except UpstreamUnavailable:
        return SourceResult(name, "UNAVAILABLE", [])
    if response.status_code != HTTPStatus.OK:
        _logger.warning(
            "bff.calendar_source_status",
            extra={"fields": {"source": name, "status": response.status_code}},
        )
        return SourceResult(name, "UNAVAILABLE", [])
    try:
        document = response.json()
        items = [to_item(item) for item in document["items"]]
    except (ValueError, KeyError, TypeError):
        return SourceResult(name, "UNAVAILABLE", [])
    return SourceResult(name, "OK", items, truncated=document.get("next_cursor") is not None)


def _compose(period: Period, results: list[SourceResult], now: datetime) -> Response:
    if all(result.status != "OK" for result in results):
        if all(result.status == "TIMEOUT" for result in results):
            return _failure(UpstreamTimeout())
        return _failure(UpstreamUnavailable())
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


def build_calendar_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    athlete_snapshot: Callable[[UUID], dict[str, Any]],
    events: EventsManagementClient,
    training: TrainingManagementClient,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/v1/me", tags=["calendar"])

    def authenticate_athlete(authorization: str | None) -> tuple[UUID, str] | Response:
        if not authorization or not authorization.lower().startswith(BEARER_PREFIX):
            return _unauthenticated()
        token = authorization[len(BEARER_PREFIX) :].strip()
        try:
            identity = verify_token(token)
            user_id = UUID(identity.subject)
        except (InvalidAccessToken, ValueError):
            return _unauthenticated()
        except JwksUnavailable:
            _logger.warning("bff.jwks_unavailable")
            return _failure(UpstreamUnavailable())
        if identity.actor_type != ATHLETE:
            return error_response(
                HTTPStatus.FORBIDDEN,
                "permission_required",
                "Su tipo de usuario no puede realizar esta operación.",
            )
        return user_id, token

    def require_capability(user_id: UUID) -> Response | None:
        try:
            snapshot = athlete_snapshot(user_id)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _failure(error)
        granted = snapshot.get("capabilities") or []
        if snapshot.get("subscription_status") == "ACTIVE" and CALENDAR_CAPABILITY in granted:
            return None
        return error_response(
            HTTPStatus.FORBIDDEN, "capability_required", "Su plan no incluye esta funcionalidad."
        )

    def gather(token: str, period: Period) -> list[SourceResult]:
        calls = [
            (
                "events",
                lambda: events.published_events(
                    token, period.period_from, period.period_to, SOURCE_LIMIT
                ),
                event_item,
            ),
            (
                "training",
                lambda: training.planned_sessions(
                    token, period.date_from, period.date_to, SOURCE_LIMIT
                ),
                session_item,
            ),
        ]
        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            # Cada hilo corre con una copia del contexto: conserva la correlación.
            futures = [
                pool.submit(contextvars.copy_context().run, _fetch, name, call, to_item)
                for name, call, to_item in calls
            ]
            return [future.result() for future in futures]

    @router.get(
        "/calendar",
        response_model=CalendarResponse,
        summary="Calendario deportivo: eventos publicados y sesiones planificadas (HU035)",
        responses={
            HTTPStatus.UNAUTHORIZED.value: {
                "model": ErrorResponse,
                "description": "`invalid_access_token`: falta el token o no es válido.",
            },
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` (no es deportista) o "
                "`capability_required` (`calendar.sport.view`).",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `invalid_datetime`, `ends_before_starts`, "
                "`out_of_range` (más de 92 días) o `required` (solo uno de los dos extremos).",
            },
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": "`upstream_unavailable`: capacidad no evaluable o ambas fuentes "
                "caídas.",
            },
            HTTPStatus.GATEWAY_TIMEOUT.value: {
                "model": ErrorResponse,
                "description": "`upstream_timeout`: capacidad o ambas fuentes sin respuesta.",
            },
        },
    )
    def calendar(
        raw_from: Annotated[str | None, Query(alias="from", description=INSTANT)] = None,
        raw_to: Annotated[str | None, Query(alias="to", description=INSTANT)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        started = time.monotonic()
        caller = authenticate_athlete(authorization)
        if isinstance(caller, Response):
            return caller
        user_id, token = caller
        try:
            period = normalize_period(raw_from, raw_to, clock())
        except InvalidPeriod as error:
            return error_response(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                error.code,
                "El periodo no es válido.",
                [ErrorDetail(field=f"query.{error.field}", issue=error.issue)],
            )
        rejection = require_capability(user_id)
        if rejection is not None:
            return rejection
        results = gather(token, period)
        _logger.info(
            "bff.calendar",
            extra={
                "fields": {
                    **{f"{result.name}_status": result.status for result in results},
                    **{f"{result.name}_items": len(result.items) for result in results},
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                }
            },
        )
        return _compose(period, results, clock())

    return router
