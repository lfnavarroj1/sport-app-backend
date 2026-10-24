"""HU024: publicación de eventos y eventos propios del organizador (ADR-033).

- `POST /v1/events/{event_id}/publish`: sin cuerpo, con `Idempotency-Key`; `200` con el
  agregado completo (`Idempotent-Replayed: true` al repetir la clave).
- `GET /v1/me/events?status&limit&cursor`: eventos propios con el mismo agregado y, desde
  HU025, `registration_count` (base de escritura, ADR-040).
El organizador sale del token; un evento ajeno o inexistente es `404 event_not_found`.
Instantes en UTC (`Z`) con la zona declarada: la conversión corresponde al frontend.
"""

from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from events_management.api.events import EventLocation
from events_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    correlation_uuid,
    forbidden,
)
from events_management.api.registrations import EventFee
from events_management.application.commands.publish_event import (
    PublishEvent,
    PublishEventCommand,
)
from events_management.application.errors import IdempotencyKeyReused, PermissionRequired
from events_management.application.ports import Caller
from events_management.application.queries.my_events import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    ListMyEvents,
    MyEventsFilters,
)
from events_management.application.queries.published_events import InvalidEventsQuery
from events_management.domain.sport_events.model import EventNotFound
from events_management.domain.sport_events.published import (
    aggregate_document,
    owned_event_document,
)
from events_management.domain.validation import InvalidSportEvent
from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"


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
    fee: Annotated[
        EventFee | None,
        Field(description="Tarifa informativa (HU025); `null` sin tarifa. No es un cobro."),
    ]
    fee_updated_at: Annotated[
        datetime | None,
        Field(description="Último cambio de la tarifa; `null` si nunca se definió."),
    ]


class OwnEventResponse(EventAggregateResponse):
    """El agregado como lo ve su organizador (HU025)."""

    registration_count: Annotated[
        int,
        Field(
            description="Inscripciones vigentes; cambiar la tarifa no las modifica (cada una "
            "conserva su `applied_fee`)."
        ),
    ]


class MyEventsResponse(BaseModel):
    items: list[OwnEventResponse]
    next_cursor: str | None = Field(
        description="Cursor opaco de la página siguiente; `null` si no hay más."
    )


_ORGANIZER_FORBIDDEN: dict[int | str, dict[str, Any]] = {
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required`: el actor no es `organizer`.",
    }
}


def _invalid(details: list[ErrorDetail], message: str = "La solicitud no es válida.") -> Response:
    return error_response(HTTPStatus.UNPROCESSABLE_ENTITY, InvalidSportEvent.code, message, details)


def _not_found() -> Response:
    return error_response(
        HTTPStatus.NOT_FOUND, EventNotFound.code, "El evento no existe o no está disponible."
    )


def _idempotency_key(raw: str | None) -> UUID | Response:
    if raw is None or not raw.strip():
        return _invalid([ErrorDetail(field=IDEMPOTENCY_HEADER, issue="required")])
    try:
        return UUID(raw.strip())
    except ValueError:
        return _invalid([ErrorDetail(field=IDEMPOTENCY_HEADER, issue="unknown_value")])


def _authorized_key(
    publish_event: PublishEvent, caller: Caller, raw: str | None
) -> UUID | Response:
    """Actor y permiso antes que la clave: un actor sin permiso recibe `403` (CA6)."""
    try:
        publish_event.authorize(caller)
    except PermissionRequired as error:
        return forbidden(error)
    return _idempotency_key(raw)


_PUBLICATION_ERRORS = (PermissionRequired, EventNotFound, InvalidSportEvent, IdempotencyKeyReused)


def _publication_failure(error: Exception) -> Response:
    if isinstance(error, PermissionRequired):
        return forbidden(error)
    if isinstance(error, EventNotFound):
        return _not_found()
    if isinstance(error, InvalidSportEvent):
        return _invalid(
            [ErrorDetail(field=item.field, issue=item.issue.value) for item in error.violations],
            "El evento no cumple los requisitos de publicación.",
        )
    return error_response(
        HTTPStatus.CONFLICT,
        IdempotencyKeyReused.code,
        "La clave de idempotencia ya se usó con otra solicitud.",
    )


def build_publication_router(
    *, verify_token: TokenVerifier, publish_event: PublishEvent, list_my_events: ListMyEvents
) -> APIRouter:
    router = APIRouter(tags=["events"])

    @router.post(
        "/v1/events/{event_id}/publish",
        response_model=EventAggregateResponse,
        summary="Publicar un evento propio (HU024)",
        description="Sin cuerpo. Solo `organizer` propietario con `events.publish`. "
        "`DRAFT` → `PUBLISHED` con versión + 1 y `published_at`; un evento ya publicado "
        "responde `200` con su estado vigente, sin nueva versión ni evento.",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.OK.value: {
                "headers": {
                    REPLAYED_HEADER: {
                        "description": "`true` cuando se devuelve el resultado de un reintento.",
                        "schema": {"type": "string"},
                    }
                }
            },
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required`: no es `organizer` con `events.publish`.",
            },
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": "`event_not_found`: inexistente o de otro organizador.",
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`idempotency_key_reused`: la clave ya se usó con otro evento.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` con `details[{field, issue}]`: requisitos "
                "de ADR-028 incumplidos (`not_in_future` para `starts_at`) o "
                "`Idempotency-Key` ausente o inválida. El evento sigue `DRAFT`.",
            },
        },
    )
    def publish(
        event_id: UUID,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        key = _authorized_key(publish_event, caller, idempotency_key)
        if isinstance(key, Response):
            return key
        try:
            result = publish_event(
                caller,
                PublishEventCommand(
                    event_id=event_id, idempotency_key=key, correlation_id=correlation_uuid()
                ),
            )
        except _PUBLICATION_ERRORS as error:
            return _publication_failure(error)
        headers = {REPLAYED_HEADER: "true"} if result.replayed else {}
        return JSONResponse(aggregate_document(result.event), headers=headers)

    @router.get(
        "/v1/me/events",
        response_model=MyEventsResponse,
        summary="Mis eventos como organizador (HU024)",
        description="Solo los eventos propios; `status` opcional (sin él, `DRAFT` y "
        "`PUBLISHED`). Orden `starts_at`, `event_id` ascendente.",
        responses={
            **AUTH_RESPONSES,
            **_ORGANIZER_FORBIDDEN,
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `status`, `limit` o `cursor` inválidos.",
            },
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
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            page = list_my_events(
                caller, MyEventsFilters(status=status, limit=limit, cursor=cursor)
            )
        except PermissionRequired as error:
            return forbidden(error)
        except InvalidEventsQuery as error:
            return _invalid(
                [ErrorDetail(field=f"query.{error.field}", issue=error.issue)],
                "La consulta no es válida.",
            )
        body = {
            "items": [
                owned_event_document(item, page.registration_count(item)) for item in page.items
            ],
            "next_cursor": page.next_cursor,
        }
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    return router
