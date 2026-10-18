"""HU024 en el canal web: publicar eventos y consultas de apoyo (contrato de ADR-033).

- `POST /v1/events/{event_id}/publish`: filtro de borde `organizer` con `events.publish`;
  la clave de idempotencia viaja tal cual y el servicio decide propiedad, requisitos
  (`422` con `details`) y transición.
- `GET /v1/me/events?status&limit&cursor`: filtro de borde `organizer`.
- `GET /v1/events/{event_id}`: cualquier actor autenticado; el servicio aplica la
  visibilidad (`DRAFT` solo para su organizador) y el BFF omite `my_registration`.
Los errores del servicio se entregan sin alterar. Instantes en UTC (`Z`) con la zona del
evento; la conversión a la zona del usuario corresponde al frontend.
"""

from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bff_web.api.events import EventLocation
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
from sportapp_platform.errors import ErrorResponse
from sportapp_platform.tokens import VerifiedAccessToken

ORGANIZER = "organizer"
PUBLISH_PERMISSION = "events.publish"
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
MAX_LIMIT = 100
DEFAULT_LIMIT = 20
SOURCE = "events-management"


class EventAggregateResponse(BaseModel):
    """Agregado completo de ADR-028 con estado, versión y `published_at` (ADR-033)."""

    event_id: UUID
    organizer_id: UUID
    name: str
    description: str
    discipline: str
    location: EventLocation
    starts_at: datetime
    ends_at: Annotated[datetime, Field(description="`starts_at` + `duration_minutes`.")]
    time_zone: str
    duration_minutes: int
    capacity: int
    status: Literal["DRAFT", "PUBLISHED"]
    version: int
    created_at: datetime
    updated_at: datetime
    published_at: Annotated[
        datetime | None, Field(description="Instante de la publicación; `null` en borrador.")
    ]


class MyEventsResponse(BaseModel):
    items: list[EventAggregateResponse]
    next_cursor: str | None = Field(
        description="Cursor opaco de la página siguiente; `null` si no hay más."
    )


class EventDetailLocation(BaseModel):
    name: str
    city: str
    country_code: str


class EventFee(BaseModel):
    amount: Annotated[str, Field(description="Cadena decimal con dos decimales.")]
    currency: str
    billing_unit: str


class EventDetailResponse(BaseModel):
    """Detalle de ADR-033 en el canal web, sin `my_registration` (propio de HU041)."""

    event_id: UUID
    name: str
    description: str
    discipline: str
    location: EventDetailLocation
    starts_at: datetime
    ends_at: datetime
    time_zone: str
    capacity: int
    status: Annotated[
        Literal["DRAFT", "PUBLISHED"],
        Field(description="`DRAFT` solo lo recibe el organizador propietario (HU024)."),
    ]
    version: int
    fee: Annotated[EventFee | None, Field(description="`null` hasta HU025.")]


def _error(status: HTTPStatus, description: str) -> dict[int | str, dict[str, Any]]:
    return {status.value: {"model": ErrorResponse, "description": description}}


def build_event_publication_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    permissions: ActorPermissions,
    events: EventsManagementClient,
    channel: str,
) -> APIRouter:
    router = APIRouter(tags=["events"])

    @router.post(
        "/v1/events/{event_id}/publish",
        response_model=EventAggregateResponse,
        summary="Publicar un evento propio (HU024)",
        description="Sin cuerpo. `DRAFT` → `PUBLISHED` con versión + 1 y `published_at`; un "
        "evento ya publicado responde `200` con su estado vigente, sin nueva versión.",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            HTTPStatus.OK.value: {
                "headers": {
                    REPLAYED_HEADER: {
                        "description": "`true` cuando se devuelve el resultado de un reintento.",
                        "schema": {"type": "string"},
                    }
                }
            },
            **_error(
                HTTPStatus.FORBIDDEN, "`permission_required` (`organizer` con `events.publish`)."
            ),
            **_error(HTTPStatus.NOT_FOUND, "`event_not_found`: inexistente o de otro organizador."),
            **_error(HTTPStatus.CONFLICT, "`idempotency_key_reused`."),
            **_error(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "`validation_error` con `details[{field, issue}]`: requisitos de ADR-028 "
                "(`not_in_future` para `starts_at`) o `Idempotency-Key` ausente o inválida.",
            ),
        },
    )
    def publish(
        event_id: UUID,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        actor = session.actor_type
        if actor != ORGANIZER or PUBLISH_PERMISSION not in permissions.for_actor(actor, channel):
            return _permission_required()
        try:
            upstream = events.publish_event(session.token, event_id, idempotency_key)
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
            headers=(REPLAYED_HEADER,),
            source=SOURCE,
        )

    @router.get(
        "/v1/me/events",
        response_model=MyEventsResponse,
        summary="Mis eventos como organizador (HU024)",
        description="Solo los propios; `status` opcional (sin él, `DRAFT` y `PUBLISHED`). "
        "Orden `starts_at`, `event_id` ascendente.",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            **_error(HTTPStatus.FORBIDDEN, "`permission_required`: el actor no es `organizer`."),
            **_error(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "`validation_error`: `status`, `limit` o `cursor` inválidos.",
            ),
        },
    )
    def my_events(
        *,
        status: Annotated[
            Literal["DRAFT", "PUBLISHED"] | None, Query(description="Filtro de estado.")
        ] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        if session.actor_type != ORGANIZER:
            return _permission_required()
        params = {"limit": str(limit)}
        if status is not None:
            params["status"] = status
        if cursor is not None:
            params["cursor"] = cursor
        try:
            upstream = events.list_my_events(session.token, params)
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
        "/v1/events/{event_id}",
        response_model=EventDetailResponse,
        summary="Detalle de un evento (ADR-033)",
        description="Un evento `PUBLISHED` es visible para cualquier actor autenticado; un "
        "`DRAFT`, solo para su organizador propietario.",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            **_error(HTTPStatus.FORBIDDEN, "`permission_required`: actor desconocido."),
            **_error(
                HTTPStatus.NOT_FOUND,
                "`event_not_found`: inexistente, o borrador de otro organizador.",
            ),
        },
    )
    def event_detail(
        event_id: UUID, authorization: Annotated[str | None, Header()] = None
    ) -> Response:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        try:
            upstream = events.get_event(session.token, event_id)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        if upstream.status_code == HTTPStatus.OK:
            body = {k: v for k, v in upstream.json().items() if k != "my_registration"}
            return JSONResponse(body, headers={"Cache-Control": "no-store"})
        return _passthrough(upstream, {HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND}, source=SOURCE)

    return router
