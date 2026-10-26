"""HU022: `GET /v1/me/events/{event_id}/registrations?status&limit&cursor` (ADR-039).

Solo el organizador propietario con `events.attendees.view`. Identidad mínima: el servicio
devuelve `athlete_id` (el nombre lo compone `bff-web` con Usuarios); nunca correo, perfil ni
datos de salud. Instantes en UTC (`Z`); `applied_fee` es informativa (`null` sin tarifa).
"""

from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, Field

from events_management.api.identity import AUTH_RESPONSES, TokenVerifier, authenticate, forbidden
from events_management.api.registrations import Fee
from events_management.application.errors import PermissionRequired
from events_management.application.queries.attendees import (
    ALL_STATUSES,
    DEFAULT_LIMIT,
    DEFAULT_STATUS,
    MAX_LIMIT,
    AttendeesFilters,
    AttendeesResult,
    InvalidAttendeesQuery,
    ListEventAttendees,
)
from events_management.domain.registrations.events import fee_document
from events_management.domain.registrations.model import EventNotAvailable
from events_management.domain.sport_events.created import utc_instant
from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response


class AttendeeItem(BaseModel):
    registration_id: UUID
    athlete_id: UUID
    status: Literal["REGISTERED"]
    registered_at: Annotated[str, Field(description="Instante en UTC con sufijo `Z`.")]
    applied_fee: Annotated[Fee | None, Field(description="Informativa: no es un cobro.")]


class AttendeesByStatus(BaseModel):
    REGISTERED: int


class AttendeesResponse(BaseModel):
    items: list[AttendeeItem]
    total: Annotated[
        int, Field(description="Inscripciones del evento que cumplen el filtro de `status`.")
    ]
    by_status: Annotated[
        AttendeesByStatus, Field(description="Todas las inscripciones del evento por estado.")
    ]
    next_cursor: str | None = Field(
        description="Cursor opaco de la página siguiente; `null` si no hay más."
    )


def _document(result: AttendeesResult) -> dict[str, Any]:
    return {
        "items": [
            {
                "registration_id": str(item.registration_id),
                "athlete_id": str(item.athlete_id),
                "status": item.status.value,
                "registered_at": utc_instant(item.registered_at),
                "applied_fee": fee_document(item.applied_fee),
            }
            for item in result.page.items
        ],
        "total": result.total,
        "by_status": result.page.by_status,
        "next_cursor": result.next_cursor,
    }


def build_attendees_router(
    *, verify_token: TokenVerifier, list_attendees: ListEventAttendees
) -> APIRouter:
    router = APIRouter(tags=["event-attendees"])

    @router.get(
        "/v1/me/events/{event_id}/registrations",
        response_model=AttendeesResponse,
        summary="Inscritos de un evento propio (HU022)",
        description="Orden `registered_at`, `registration_id` ascendente con cursor keyset. "
        "Sin inscritos: `items` vacío y `total` 0. Identidad mínima: sin correo ni perfil.",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required`: no es `organizer` con "
                "`events.attendees.view`.",
            },
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": f"`{EventNotAvailable.code}`: el evento no existe o es de otro "
                "organizador.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `query.status` `unknown_value`, "
                "`query.limit` `out_of_range` o `query.cursor` `invalid_cursor`.",
            },
        },
    )
    def event_attendees(
        *,
        event_id: UUID,
        response: Response,
        status: Annotated[
            str | None,
            Query(
                description=f"`{DEFAULT_STATUS}` (por defecto) o `{ALL_STATUSES}`.",
                max_length=32,
            ),
        ] = None,
        limit: Annotated[
            str | None, Query(description=f"1 a {MAX_LIMIT}; por defecto {DEFAULT_LIMIT}.")
        ] = None,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | AttendeesResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            parsed_limit = int(limit) if limit is not None else DEFAULT_LIMIT
        except ValueError:
            parsed_limit = 0  # Fuera de rango: lo informa la consulta como `out_of_range`.
        try:
            result = list_attendees(
                caller,
                event_id,
                AttendeesFilters(status=status, limit=parsed_limit, cursor=cursor),
            )
        except PermissionRequired as error:
            return forbidden(error)
        except EventNotAvailable as error:
            return error_response(
                HTTPStatus.NOT_FOUND,
                error.code,
                "El evento no existe o no está disponible.",
            )
        except InvalidAttendeesQuery as error:
            return error_response(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                error.code,
                "La consulta no es válida.",
                [ErrorDetail(field=f"query.{error.field}", issue=error.issue)],
            )
        response.headers["Cache-Control"] = "no-store"
        return AttendeesResponse.model_validate(_document(result))

    return router
