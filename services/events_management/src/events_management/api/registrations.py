"""HU041: detalle de evento publicado, inscripción y «mis inscripciones» (ADR-032).

- `GET /v1/events/{event_id}`: evento visible (ADR-033: `PUBLISHED` para cualquier actor
  autenticado, `DRAFT` solo para su organizador) con `my_registration` del deportista.
- `POST /v1/events/{event_id}/registrations`: sin cuerpo, con `Idempotency-Key`; `201` con
  `Location: /v1/me/event-registrations/{registration_id}`.
- `GET /v1/me/event-registrations?status&limit&cursor`.
Inscribirse y listar: solo `athlete`; el deportista sale del token y `athlete_id` no
viaja en las respuestas.
Instantes en UTC (`Z`); `fee` y `applied_fee` son informativos (`null` sin HU025).
"""

from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from events_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    correlation_uuid,
    forbidden,
)
from events_management.application.commands.register_for_event import (
    RegisterForEvent,
    RegisterForEventCommand,
)
from events_management.application.errors import (
    EventRegistrationExists,
    IdempotencyKeyReused,
    PermissionRequired,
)
from events_management.application.ports import EventDetailView
from events_management.application.queries.registrations import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    GetPublishedEvent,
    InvalidRegistrationsQuery,
    ListMyRegistrations,
    RegistrationsFilters,
)
from events_management.application.registration_documents import registration_document
from events_management.domain.registrations.model import (
    EventFull,
    EventNotAvailable,
    EventRegistrationClosed,
)
from events_management.domain.sport_events.created import utc_instant
from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"


class RegistrationLocation(BaseModel):
    name: str
    city: str
    country_code: str


class Fee(BaseModel):
    amount: Annotated[str, Field(description="Cadena decimal con dos decimales.")]
    currency: str


class EventFee(Fee):
    billing_unit: str


class RegistrationEvent(BaseModel):
    name: str
    starts_at: datetime
    ends_at: datetime
    time_zone: str
    location: RegistrationLocation


class RegistrationResponse(BaseModel):
    registration_id: UUID
    event_id: UUID
    status: Literal["REGISTERED"]
    registered_at: datetime
    applied_fee: Annotated[Fee | None, Field(description="Informativa: no es un cobro.")]
    version: int
    event: RegistrationEvent


class RegistrationsResponse(BaseModel):
    items: list[RegistrationResponse]
    next_cursor: str | None


class MyRegistration(BaseModel):
    registration_id: UUID
    status: Literal["REGISTERED"]
    registered_at: datetime


class EventDetailResponse(BaseModel):
    event_id: UUID
    name: str
    description: str
    discipline: str
    location: RegistrationLocation
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
    my_registration: MyRegistration | None


def _detail_document(view: EventDetailView) -> dict[str, object]:
    event, mine = view.event, view.my_registration
    return {
        "event_id": str(event.event_id),
        "name": event.name,
        "description": view.description,
        "discipline": event.discipline,
        "location": {
            "name": event.location_name,
            "city": event.location_city,
            "country_code": event.location_country_code,
        },
        "starts_at": utc_instant(event.starts_at),
        "ends_at": utc_instant(event.ends_at),
        "time_zone": event.time_zone,
        "capacity": view.capacity,
        "status": event.status,
        "version": event.version,
        "fee": None,
        "my_registration": None
        if mine is None
        else {
            "registration_id": str(mine.registration_id),
            "status": mine.status.value,
            "registered_at": utc_instant(mine.registered_at),
        },
    }


def _not_found() -> Response:
    return error_response(
        HTTPStatus.NOT_FOUND, EventNotAvailable.code, "El evento no existe o no está disponible."
    )


def _invalid(field: str, issue: str) -> Response:
    return error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "validation_error",
        "La solicitud no es válida.",
        [ErrorDetail(field=field, issue=issue)],
    )


_DETAIL_AUTH_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: response
    for code, response in AUTH_RESPONSES.items()
    if code != HTTPStatus.FORBIDDEN.value
}
_ATHLETE_AUTH_RESPONSES: dict[int | str, dict[str, Any]] = {
    **AUTH_RESPONSES,
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required`: solo el deportista.",
    },
}
_CONFLICT_MESSAGES = {
    EventRegistrationClosed.code: "El evento ya inició; la inscripción está cerrada.",
    EventFull.code: "El evento no tiene cupos disponibles.",
    IdempotencyKeyReused.code: "La clave de idempotencia ya se usó con otra solicitud.",
}


_REGISTRATION_ERRORS = (
    PermissionRequired,
    EventNotAvailable,
    EventRegistrationExists,
    EventRegistrationClosed,
    EventFull,
    IdempotencyKeyReused,
)


def _idempotency_key(raw: str | None) -> UUID | Response:
    if raw is None or not raw.strip():
        return _invalid(IDEMPOTENCY_HEADER, "required")
    try:
        return UUID(raw.strip())
    except ValueError:
        return _invalid(IDEMPOTENCY_HEADER, "unknown_value")


def _registration_failure(error: Exception) -> Response:
    if isinstance(error, PermissionRequired):
        return forbidden(error)
    if isinstance(error, EventNotAvailable):
        return _not_found()
    if isinstance(error, EventRegistrationExists):
        return error_response(
            HTTPStatus.CONFLICT,
            error.code,
            "Ya estás inscrito a este evento.",
            [ErrorDetail(field="registration_id", issue=str(error.registration_id))],
        )
    code = getattr(error, "code", "")
    return error_response(HTTPStatus.CONFLICT, code, _CONFLICT_MESSAGES[code])


def build_registrations_router(
    *,
    verify_token: TokenVerifier,
    get_event: GetPublishedEvent,
    register: RegisterForEvent,
    list_registrations: ListMyRegistrations,
) -> APIRouter:
    router = APIRouter(tags=["event-registrations"])

    @router.get(
        "/v1/events/{event_id}",
        response_model=EventDetailResponse,
        summary="Detalle de un evento (ADR-033) con mi inscripción (HU041)",
        description="Un evento `PUBLISHED` es visible para cualquier actor autenticado; un "
        "`DRAFT`, solo para su organizador propietario (HU024). `my_registration` solo se "
        "compone para el deportista y es `null` para los demás actores.",
        responses={
            **_DETAIL_AUTH_RESPONSES,
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": "`event_not_found`: inexistente, o borrador de otro organizador.",
            },
        },
    )
    def event_detail(
        event_id: UUID, authorization: Annotated[str | None, Header()] = None
    ) -> Response:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            view = get_event(caller, event_id)
        except EventNotAvailable:
            return _not_found()
        return JSONResponse(_detail_document(view), headers={"Cache-Control": "no-store"})

    @router.post(
        "/v1/events/{event_id}/registrations",
        status_code=HTTPStatus.CREATED,
        response_model=RegistrationResponse,
        summary="Inscribirme a un evento publicado (HU041)",
        responses={
            **_ATHLETE_AUTH_RESPONSES,
            HTTPStatus.CREATED.value: {
                "headers": {
                    "Location": {"schema": {"type": "string"}},
                    REPLAYED_HEADER: {
                        "description": "`true` cuando se devuelve el resultado de un reintento.",
                        "schema": {"type": "string"},
                    },
                }
            },
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": "`event_not_found`: inexistente o no publicado.",
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`event_registration_exists` (con `registration_id` en `details`), "
                "`event_full`, `event_registration_closed` o `idempotency_key_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `Idempotency-Key` ausente o inválida.",
            },
        },
    )
    def register_for_event(
        event_id: UUID,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        if caller.actor_type != "athlete":
            return forbidden(PermissionRequired())
        key = _idempotency_key(idempotency_key)
        if isinstance(key, Response):
            return key
        try:
            result = register(
                caller,
                RegisterForEventCommand(
                    event_id=event_id, idempotency_key=key, correlation_id=correlation_uuid()
                ),
            )
        except _REGISTRATION_ERRORS as error:
            return _registration_failure(error)
        registration_id = result.registration.registration.registration_id
        headers = {"Location": f"/v1/me/event-registrations/{registration_id}"}
        if result.replayed:
            headers[REPLAYED_HEADER] = "true"
        return JSONResponse(
            registration_document(result.registration),
            status_code=HTTPStatus.CREATED,
            headers=headers,
        )

    @router.get(
        "/v1/me/event-registrations",
        response_model=RegistrationsResponse,
        summary="Mis inscripciones a eventos (HU041)",
        responses={
            **_ATHLETE_AUTH_RESPONSES,
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `status`, `limit` o `cursor` inválidos.",
            },
        },
    )
    def my_registrations(
        *,
        status: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query()] = DEFAULT_LIMIT,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            page = list_registrations(
                caller, RegistrationsFilters(status=status, limit=limit, cursor=cursor)
            )
        except PermissionRequired as error:
            return forbidden(error)
        except InvalidRegistrationsQuery as error:
            return _invalid(f"query.{error.field}", error.issue)
        body = {
            "items": [registration_document(item) for item in page.items],
            "next_cursor": page.next_cursor,
        }
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    return router


__all__ = ["MAX_LIMIT", "build_registrations_router"]
