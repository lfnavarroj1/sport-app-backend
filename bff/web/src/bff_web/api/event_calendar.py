"""HU023 en el canal web: calendario del organizador y coincidencias (ADR-040).

- `GET /v1/events?from&to&discipline&city&limit&cursor`: filtro de borde `organizer` con
  `events.calendar.view`; los parámetros y los errores del servicio viajan sin alterar.
- `GET /v1/me/events/{event_id}/conflicts`: el BFF busca el evento propio (`DRAFT` o
  `PUBLISHED`) en `GET /v1/me/events` (decisión del responsable del 2026-10-20: el detalle
  público no prueba la propiedad) y consulta los publicados de su periodo con el filtro de
  disciplina y, aparte, con el de ciudad. La normalización la aplica el servicio, así que el
  BFF no la duplica. Hay coincidencia cuando los periodos se solapan de forma estricta
  (`ends_at == starts_at` no coincide). El propio evento se excluye. La respuesta es
  informativa: no bloquea publicar ni reserva fechas.
Instantes en UTC (`Z`) con la zona del evento; la conversión corresponde al frontend.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, Field

from bff_web.api.training import (
    _UNAUTHORIZED,
    _UPSTREAM,
    _passthrough,
    _permission_required,
    _upstream_failure,
    authenticate_session,
)
from bff_web.clients.events_management import EventsManagementClient
from bff_web.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.authorization import ActorPermissions
from sportapp_platform.errors import ErrorResponse, error_response
from sportapp_platform.tokens import VerifiedAccessToken

ORGANIZER = "organizer"
CALENDAR_PERMISSION = "events.calendar.view"
SOURCE = "events-management"
# Límites del servicio: `GET /v1/events` (ADR-031) y `GET /v1/me/events` (ADR-033).
MAX_LIMIT = 200
DEFAULT_LIMIT = 200
MY_EVENTS_LIMIT = 100
INSTANT = "Instante ISO 8601 con desfase UTC explícito."
TERM = (
    "Opcional. Igualdad sin distinguir mayúsculas ni acentos sobre el texto recortado "
    "(2 a 80 caracteres)."
)
Reason = Literal["SAME_DISCIPLINE", "SAME_CITY"]
REASONS: tuple[tuple[Reason, str], ...] = (("SAME_DISCIPLINE", "discipline"), ("SAME_CITY", "city"))


class PublishedEventLocation(BaseModel):
    name: str
    city: str
    country_code: str


class PublishedEventItem(BaseModel):
    event_id: UUID
    name: str
    discipline: str
    location: PublishedEventLocation
    starts_at: datetime
    ends_at: datetime
    time_zone: str
    status: Literal["PUBLISHED"]
    version: int


class PublishedEventsResponse(BaseModel):
    items: list[PublishedEventItem]
    next_cursor: str | None = Field(
        description="Cursor opaco de la página siguiente; `null` si no hay más."
    )


class EventConflict(BaseModel):
    event_id: UUID
    name: str
    discipline: str
    city: str
    starts_at: datetime
    ends_at: datetime
    time_zone: str
    reasons: Annotated[
        list[Reason], Field(description="Uno o ambos, en el orden `SAME_DISCIPLINE`, `SAME_CITY`.")
    ]


class EventConflictsResponse(BaseModel):
    conflicts: Annotated[
        list[EventConflict], Field(description="Orden `starts_at`, `event_id` ascendente.")
    ]
    evaluated_at: Annotated[datetime, Field(description="Instante de la evaluación, en UTC.")]


class _Stop(Exception):
    """Corta la composición con la respuesta que corresponde entregar."""

    def __init__(self, response: Response) -> None:
        self.response = response


def _error(status: HTTPStatus, description: str) -> dict[int | str, dict[str, Any]]:
    return {status.value: {"model": ErrorResponse, "description": description}}


def _page(upstream: Any) -> dict[str, Any]:
    """Página del servicio; `403` se entrega tal cual y cualquier otro estado es una falla."""
    if upstream.status_code == HTTPStatus.OK:
        page: dict[str, Any] = upstream.json()
        return page
    raise _Stop(_passthrough(upstream, {HTTPStatus.FORBIDDEN}, source=SOURCE))


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


def build_event_calendar_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    permissions: ActorPermissions,
    events: EventsManagementClient,
    channel: str,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(tags=["events"])

    def authorize(authorization: str | None) -> Any:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        actor = session.actor_type
        if actor != ORGANIZER or CALENDAR_PERMISSION not in permissions.for_actor(actor, channel):
            return _permission_required()
        return session

    def own_event(token: str, event_id: UUID) -> dict[str, Any] | None:
        params = {"limit": str(MY_EVENTS_LIMIT)}
        while True:
            page = _page(events.list_my_events(token, params))
            for item in page["items"]:
                if UUID(item["event_id"]) == event_id:
                    found: dict[str, Any] = item
                    return found
            if page["next_cursor"] is None:
                return None
            params = {"limit": str(MY_EVENTS_LIMIT), "cursor": page["next_cursor"]}

    def published(token: str, own: dict[str, Any], **term: str) -> list[dict[str, Any]]:
        """Todas las páginas de publicados del periodo del evento propio con un filtro."""
        base = {"from": own["starts_at"], "to": own["ends_at"], "limit": str(MAX_LIMIT), **term}
        params, items = base, []
        while True:
            page = _page(events.list_events(token, params))
            items += page["items"]
            if page["next_cursor"] is None:
                return items
            params = {**base, "cursor": page["next_cursor"]}

    @router.get(
        "/v1/events",
        response_model=PublishedEventsResponse,
        summary="Calendario de eventos publicados (HU023)",
        description="`organizer` con `events.calendar.view`. Solo `PUBLISHED`, sin "
        "`organizer_id`. Periodo de hasta 92 días con `from` anterior a `to`; `discipline` y "
        "`city` sin distinguir mayúsculas ni acentos. Orden `starts_at`, `event_id` "
        "ascendente; el cursor se reenvía con los mismos filtros.",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            **_error(
                HTTPStatus.FORBIDDEN,
                "`permission_required` (`organizer` con `events.calendar.view`).",
            ),
            **_error(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "`validation_error` en `query.<parámetro>`: `invalid_datetime`, "
                "`ends_before_starts`, `out_of_range` (más de 92 días o límite), "
                "`invalid_length` (filtro) o `invalid_cursor`.",
            ),
        },
    )
    def calendar(
        *,
        period_from: Annotated[str, Query(alias="from", description=INSTANT)],
        period_to: Annotated[str, Query(alias="to", description=INSTANT)],
        discipline: Annotated[str | None, Query(description=TERM)] = None,
        city: Annotated[str | None, Query(description=TERM)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = authorize(authorization)
        if isinstance(session, Response):
            return session
        params = {"from": period_from, "to": period_to, "limit": str(limit)}
        optional = {"discipline": discipline, "city": city, "cursor": cursor}
        params.update({name: value for name, value in optional.items() if value is not None})
        try:
            upstream = events.list_events(session.token, params)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        response = _passthrough(
            upstream,
            {HTTPStatus.OK, HTTPStatus.FORBIDDEN, HTTPStatus.UNPROCESSABLE_ENTITY},
            source=SOURCE,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get(
        "/v1/me/events/{event_id}/conflicts",
        response_model=EventConflictsResponse,
        summary="Coincidencias de un evento propio con eventos publicados (HU023)",
        description="Eventos publicados cuyo periodo se solapa con el del evento propio "
        "(`DRAFT` o `PUBLISHED`) y que comparten disciplina o ciudad normalizadas. Un borde "
        "común (`ends_at == starts_at`) no coincide; el propio evento se excluye. Es "
        "información de apoyo: no bloquea publicar ni reserva fechas.",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            **_error(
                HTTPStatus.FORBIDDEN,
                "`permission_required` (`organizer` con `events.calendar.view`).",
            ),
            **_error(HTTPStatus.NOT_FOUND, "`event_not_found`: inexistente o de otro organizador."),
        },
    )
    def conflicts(
        event_id: UUID,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | EventConflictsResponse:
        session = authorize(authorization)
        if isinstance(session, Response):
            return session
        try:
            own = own_event(session.token, event_id)
            if own is None:
                return error_response(
                    HTTPStatus.NOT_FOUND,
                    "event_not_found",
                    "El evento no existe o no está disponible.",
                )
            evaluated_at = clock()
            candidates = {
                reason: published(session.token, own, **{term: value})
                for (reason, term), value in zip(
                    REASONS, (own["discipline"], own["location"]["city"]), strict=True
                )
            }
        except _Stop as stop:
            return stop.response
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        response.headers["Cache-Control"] = "no-store"
        return EventConflictsResponse(
            conflicts=_conflicts(own, candidates), evaluated_at=evaluated_at
        )

    return router


def _conflicts(
    own: dict[str, Any], candidates: dict[Reason, list[dict[str, Any]]]
) -> list[EventConflict]:
    """Solapamiento estricto, sin el propio evento, con los motivos de cada coincidencia."""
    starts_at, ends_at = _instant(own["starts_at"]), _instant(own["ends_at"])
    found: dict[UUID, tuple[dict[str, Any], list[Reason]]] = {}
    for reason, _ in REASONS:
        for item in candidates[reason]:
            overlaps = (
                _instant(item["starts_at"]) < ends_at and _instant(item["ends_at"]) > starts_at
            )
            event_id = UUID(item["event_id"])
            if not overlaps or event_id == UUID(own["event_id"]):
                continue
            found.setdefault(event_id, (item, []))[1].append(reason)
    ordered = sorted(
        found.values(),
        key=lambda entry: (_instant(entry[0]["starts_at"]), UUID(entry[0]["event_id"])),
    )
    return [
        EventConflict(
            event_id=item["event_id"],
            name=item["name"],
            discipline=item["discipline"],
            city=item["location"]["city"],
            starts_at=item["starts_at"],
            ends_at=item["ends_at"],
            time_zone=item["time_zone"],
            reasons=reasons,
        )
        for item, reasons in ordered
    ]
