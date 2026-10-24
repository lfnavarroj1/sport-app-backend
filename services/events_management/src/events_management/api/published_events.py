"""`GET /v1/events?from&to&discipline&city&limit&cursor` (ADR-031, ampliado por ADR-040 y
ADR-039).

Lo consumen `bff-mobile` (calendario del deportista, HU035) y `bff-web` (calendario del
organizador y coincidencias, HU023; fuente `EVENT_PUBLISHED` del calendario integrado del
profesional, HU018). Solo eventos `PUBLISHED`; nunca borradores ni
`organizer_id`. Instantes en UTC (`Z`) con la zona del evento.
"""

from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, Field

from events_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    forbidden,
)
from events_management.api.registrations import EventFee
from events_management.application.errors import (
    CapabilitiesUnavailable,
    CapabilityRequired,
    PermissionRequired,
)
from events_management.application.queries.published_events import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    EventsFilters,
    EventsPage,
    InvalidEventsQuery,
    ListPublishedEvents,
)
from events_management.domain.sport_events.fee import fee_document
from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response

INSTANT = "Instante ISO 8601 con desfase UTC explícito."
TERM = (
    "Opcional. Igualdad sin distinguir mayúsculas ni acentos sobre el texto recortado "
    "(2 a 80 caracteres)."
)


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
    fee: Annotated[EventFee | None, Field(description="Informativa; `null` sin tarifa (HU025).")]


class PublishedEventsResponse(BaseModel):
    items: list[PublishedEventItem]
    next_cursor: str | None = Field(
        description="Cursor opaco de la página siguiente; `null` si no hay más."
    )

    @classmethod
    def from_page(cls, page: EventsPage) -> "PublishedEventsResponse":
        return cls(
            items=[
                PublishedEventItem(
                    event_id=item.event_id,
                    name=item.name,
                    discipline=item.discipline,
                    location=PublishedEventLocation(
                        name=item.location_name,
                        city=item.location_city,
                        country_code=item.location_country_code,
                    ),
                    starts_at=item.starts_at,
                    ends_at=item.ends_at,
                    time_zone=item.time_zone,
                    status="PUBLISHED",
                    version=item.version,
                    fee=EventFee.model_validate(fee_document(item.fee)) if item.fee else None,
                )
                for item in page.items
            ],
            next_cursor=page.next_cursor,
        )


def _invalid(field: str, issue: str) -> Response:
    return error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        InvalidEventsQuery.code,
        "La consulta no es válida.",
        [ErrorDetail(field=f"query.{field}", issue=issue)],
    )


def build_published_events_router(
    *, verify_token: TokenVerifier, list_events: ListPublishedEvents
) -> APIRouter:
    router = APIRouter(prefix="/v1/events", tags=["events"])

    @router.get(
        "",
        response_model=PublishedEventsResponse,
        summary="Eventos publicados por periodo (HU035 deportista, HU023 organizador, HU018 "
        "profesional)",
        description="`athlete` con `calendar.sport.view` (Afiliación), `organizer` con "
        "`events.calendar.view` o `service_provider` con `calendar.integrated.view`. Periodo de "
        "hasta 92 días; para el organizador `from` debe ser anterior a `to`. Orden `starts_at`, "
        "`event_id` ascendente; el cursor se reenvía con los mismos filtros.",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` (otro actor, organizador sin "
                "`events.calendar.view` o profesional sin `calendar.integrated.view`) o "
                "`capability_required` (`calendar.sport.view`).",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` en `query.<parámetro>`: `invalid_datetime`, "
                "`ends_before_starts`, `out_of_range` (más de 92 días o límite), "
                "`invalid_length` (filtro) o `invalid_cursor`.",
            },
        },
    )
    def published_events(
        *,
        response: Response,
        period_from: Annotated[datetime, Query(alias="from", description=INSTANT)],
        period_to: Annotated[datetime, Query(alias="to", description=INSTANT)],
        discipline: Annotated[str | None, Query(description=TERM)] = None,
        city: Annotated[str | None, Query(description=TERM)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | PublishedEventsResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            page = list_events(
                caller,
                EventsFilters(
                    period_from=period_from,
                    period_to=period_to,
                    limit=limit,
                    cursor=cursor,
                    discipline=discipline,
                    city=city,
                ),
            )
        except (PermissionRequired, CapabilityRequired) as error:
            return forbidden(error)
        except InvalidEventsQuery as error:
            return _invalid(error.field, error.issue)
        except CapabilitiesUnavailable:
            # El contrato interno responde `503` también ante un timeout (ADR-031).
            return error_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                CapabilitiesUnavailable.code,
                "No fue posible verificar su plan. Intente de nuevo.",
            )
        response.headers["Cache-Control"] = "no-store"
        return PublishedEventsResponse.from_page(page)

    return router
