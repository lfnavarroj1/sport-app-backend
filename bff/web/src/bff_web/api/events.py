"""HU021 en el canal web: `POST /v1/events` (contrato de ADR-028).

El BFF autentica, aplica el filtro de borde por actor (`organizer`) y permiso
(`events.create`) y reenvía el cuerpo y la clave de idempotencia sin reinterpretarlos:
las reglas del evento y los errores `422 validation_error` con `details[{field, issue}]`
los decide el servicio propietario y se entregan sin alterar. Los instantes llegan en UTC
(`Z`) con la zona declarada; la conversión a la zona del usuario corresponde al frontend.
"""

from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Header, Response
from pydantic import BaseModel, ConfigDict, Field

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
CREATE_PERMISSION = "events.create"
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
TRIMMED = "Se recortan los espacios exteriores."


class EventLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=3, max_length=120, description=TRIMMED)]
    address: Annotated[str, Field(min_length=5, max_length=200, description=TRIMMED)]
    city: Annotated[str, Field(min_length=2, max_length=80, description=TRIMMED)]
    country_code: Annotated[
        str,
        Field(
            pattern=r"^[A-Z]{2}$",
            description="ISO 3166-1 alfa-2 en mayúsculas.",
            examples=["CO"],
        ),
    ]


class CreateEventRequest(BaseModel):
    """Esquema documentado del cuerpo; la validación la hace el servicio (ADR-028)."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=3, max_length=120, description=TRIMMED)]
    description: Annotated[str, Field(min_length=20, max_length=2000, description=TRIMMED)]
    discipline: Annotated[
        str,
        Field(
            min_length=2,
            max_length=80,
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
    duration_minutes: Annotated[int, Field(ge=15, le=1440)]
    capacity: Annotated[int, Field(ge=1, le=100_000)]


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


def build_events_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    permissions: ActorPermissions,
    events: EventsManagementClient,
    channel: str,
) -> APIRouter:
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
            **_UNAUTHORIZED,
            **_UPSTREAM,
            HTTPStatus.CREATED.value: {
                "headers": {
                    "Location": {"schema": {"type": "string"}},
                    REPLAYED_HEADER: {
                        "description": "`true` cuando se devuelve el resultado de un reintento.",
                        "schema": {"type": "string"},
                    },
                }
            },
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` (`organizer` con `events.create`).",
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
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        actor = session.actor_type
        if actor != ORGANIZER or CREATE_PERMISSION not in permissions.for_actor(actor, channel):
            return _permission_required()
        try:
            upstream = events.create_event(session.token, body, idempotency_key)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        return _passthrough(
            upstream,
            {
                HTTPStatus.CREATED,
                HTTPStatus.FORBIDDEN,
                HTTPStatus.CONFLICT,
                HTTPStatus.UNPROCESSABLE_ENTITY,
            },
            headers=("Location", REPLAYED_HEADER),
        )

    return router
