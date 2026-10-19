"""HU021: `POST /v1/events` (contrato de ADR-028).

El cuerpo se lee como JSON sin validación automática para responder los errores con el
catálogo de ADR-028 (`field` con rutas como `location.country_code` e `issue` estable);
su esquema se documenta en el OpenAPI. Los instantes se responden siempre en UTC (`Z`)
junto con la zona declarada: la conversión a la zona del usuario corresponde al frontend.
"""

from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Header, Response
from pydantic import BaseModel, ConfigDict, Field

from events_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    correlation_uuid,
    forbidden,
)
from events_management.api.payload import parse_event
from events_management.application.commands.create_event import CreateEvent, CreateEventCommand
from events_management.application.errors import IdempotencyKeyReused, PermissionRequired
from events_management.domain.sport_events.created import event_document
from events_management.domain.sport_events.model import (
    ADDRESS_MAX,
    ADDRESS_MIN,
    CAPACITY_MAX,
    CAPACITY_MIN,
    CITY_MAX,
    CITY_MIN,
    DESCRIPTION_MAX,
    DESCRIPTION_MIN,
    DISCIPLINE_MAX,
    DISCIPLINE_MIN,
    DURATION_MAX,
    DURATION_MIN,
    LOCATION_NAME_MAX,
    LOCATION_NAME_MIN,
    NAME_MAX,
    NAME_MIN,
)
from events_management.domain.validation import InvalidSportEvent, Issue, Violation
from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
TRIMMED = "Se recortan los espacios exteriores."


class EventLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[
        str, Field(min_length=LOCATION_NAME_MIN, max_length=LOCATION_NAME_MAX, description=TRIMMED)
    ]
    address: Annotated[
        str, Field(min_length=ADDRESS_MIN, max_length=ADDRESS_MAX, description=TRIMMED)
    ]
    city: Annotated[str, Field(min_length=CITY_MIN, max_length=CITY_MAX, description=TRIMMED)]
    country_code: Annotated[
        str,
        Field(
            pattern=r"^[A-Z]{2}$",
            description="ISO 3166-1 alfa-2 en mayúsculas.",
            examples=["CO"],
        ),
    ]


class CreateEventRequest(BaseModel):
    """Esquema documentado del cuerpo; la validación la hacen el transporte y el dominio."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=NAME_MIN, max_length=NAME_MAX, description=TRIMMED)]
    description: Annotated[
        str, Field(min_length=DESCRIPTION_MIN, max_length=DESCRIPTION_MAX, description=TRIMMED)
    ]
    discipline: Annotated[
        str,
        Field(
            min_length=DISCIPLINE_MIN,
            max_length=DISCIPLINE_MAX,
            description="Texto libre en el MVP (sin catálogo). " + TRIMMED,
        ),
    ]
    location: EventLocation
    starts_at: Annotated[
        datetime,
        Field(
            description="Instante futuro ISO 8601 con desfase UTC explícito, coherente con "
            "`time_zone` en ese instante.",
            examples=["2026-11-15T07:00:00-05:00"],
        ),
    ]
    time_zone: Annotated[
        str, Field(description="Zona IANA declarada.", examples=["America/Bogota"])
    ]
    duration_minutes: Annotated[int, Field(ge=DURATION_MIN, le=DURATION_MAX)]
    capacity: Annotated[int, Field(ge=CAPACITY_MIN, le=CAPACITY_MAX)]


class EventResponse(BaseModel):
    """Borrador confirmado; `starts_at`, `ends_at` y las marcas de tiempo en UTC (`Z`)."""

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
    status: Literal["DRAFT"]
    version: int
    created_at: datetime
    updated_at: datetime


def _request_schema() -> dict[str, Any]:
    schema = CreateEventRequest.model_json_schema(ref_template="#/components/schemas/{model}")
    schema.pop("$defs", None)
    return schema


def _invalid(violations: list[Violation] | tuple[Violation, ...]) -> Response:
    return error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        InvalidSportEvent.code,
        "La solicitud no es válida.",
        [ErrorDetail(field=item.field, issue=item.issue.value) for item in violations],
    )


def _idempotency_key(raw: str | None) -> tuple[UUID | None, Violation | None]:
    if raw is None or not raw.strip():
        return None, Violation(IDEMPOTENCY_HEADER, Issue.REQUIRED)
    try:
        return UUID(raw.strip()), None
    except ValueError:
        return None, Violation(IDEMPOTENCY_HEADER, Issue.UNKNOWN_VALUE)


def build_events_router(*, verify_token: TokenVerifier, create_event: CreateEvent) -> APIRouter:
    router = APIRouter(prefix="/v1/events", tags=["events"])

    @router.post(
        "",
        status_code=HTTPStatus.CREATED,
        response_model=EventResponse,
        summary="Crear un evento masivo en borrador (HU021)",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": _request_schema()}},
            }
        },
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.CREATED.value: {
                "headers": {
                    "Location": {"schema": {"type": "string"}},
                    REPLAYED_HEADER: {
                        "description": "`true` cuando se devuelve el resultado de un reintento.",
                        "schema": {"type": "string"},
                    },
                }
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`idempotency_key_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` con `details[{field, issue}]` (ADR-028).",
            },
        },
    )
    def create(
        response: Response,
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | EventResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        event = parse_event(body)
        key, key_problem = _idempotency_key(idempotency_key)
        try:
            if key is None:
                # La clave es inválida: se informan también las reglas del evento.
                problems = [key_problem] if key_problem else []
                try:
                    create_event.validate(caller, event)
                except InvalidSportEvent as error:
                    problems.extend(error.violations)
                return _invalid(problems)
            result = create_event(
                caller,
                CreateEventCommand(
                    event=event, idempotency_key=key, correlation_id=correlation_uuid()
                ),
            )
        except PermissionRequired as error:
            return forbidden(error)
        except InvalidSportEvent as error:
            return _invalid(error.violations)
        except IdempotencyKeyReused as error:
            return error_response(
                HTTPStatus.CONFLICT,
                error.code,
                "La clave de idempotencia ya se usó con otra solicitud.",
            )
        response.headers["Location"] = f"/v1/events/{result.event.event_id}"
        if result.replayed:
            response.headers[REPLAYED_HEADER] = "true"
        return EventResponse.model_validate(event_document(result.event))

    return router
