"""HU022 en el canal web: inscritos de un evento propio con identidad mínima (ADR-039).

`GET /v1/me/events/{event_id}/registrations?status&limit&cursor`:
1. Filtro de borde: `organizer` con `events.attendees.view`.
2. Reenvía la consulta a `events-management` (que vuelve a verificar permiso y propiedad);
   sus errores (`403`, `404 event_not_found`, `422`) se entregan sin alterar.
3. Compone `display_name` con el contrato interno `POST /v1/users/directory-lookup` (solo
   `full_name`) para los `athlete_id` de la página. Si Usuarios no responde a tiempo o falla,
   la lista se entrega igual con `display_name: null` y `sources[users] = UNAVAILABLE` (CA6).
Nunca correo, perfil ni datos de salud. Los nombres no se registran en logs.
"""

import logging
from collections.abc import Callable
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import JSONResponse
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
from bff_web.clients.users_management import (
    UpstreamTimeout,
    UpstreamUnavailable,
    UsersManagementClient,
)
from sportapp_platform.authorization import ActorPermissions
from sportapp_platform.errors import ErrorResponse
from sportapp_platform.tokens import VerifiedAccessToken

ORGANIZER = "organizer"
ATTENDEES_PERMISSION = "events.attendees.view"
SOURCE = "events-management"
USERS_SOURCE = "users"

_logger = logging.getLogger("sportapp.bff")


class AppliedFee(BaseModel):
    amount: Annotated[str, Field(description="Cadena decimal con dos decimales.")]
    currency: str


class AttendeeItem(BaseModel):
    registration_id: UUID
    athlete_id: UUID
    display_name: Annotated[
        str | None,
        Field(description="Nombre visible (`full_name`); `null` si Usuarios no lo entregó."),
    ]
    status: Literal["REGISTERED"]
    registered_at: Annotated[str, Field(description="Instante en UTC con sufijo `Z`.")]
    applied_fee: Annotated[AppliedFee | None, Field(description="Informativa: no es un cobro.")]


class AttendeesByStatus(BaseModel):
    REGISTERED: int


class AttendeesSource(BaseModel):
    source: Literal["users"]
    status: Literal["OK", "UNAVAILABLE"]


class EventAttendeesResponse(BaseModel):
    items: list[AttendeeItem]
    total: Annotated[
        int, Field(description="Inscripciones del evento que cumplen el filtro de `status`.")
    ]
    by_status: Annotated[
        AttendeesByStatus, Field(description="Todas las inscripciones del evento por estado.")
    ]
    next_cursor: str | None
    sources: Annotated[
        list[AttendeesSource],
        Field(description="Estado de la consulta de nombres a Usuarios."),
    ]


def _names(users: UsersManagementClient, athlete_ids: list[str]) -> dict[str, str] | None:
    """Nombres por `user_id`; `None` si Usuarios no está disponible."""
    if not athlete_ids:
        return {}
    try:
        items = users.directory_lookup([UUID(value) for value in athlete_ids])
    except (UpstreamTimeout, UpstreamUnavailable, KeyError, TypeError, ValueError) as error:
        _logger.warning(
            "bff.directory_lookup_unavailable",
            extra={"fields": {"reason": type(error).__name__}},
        )
        return None
    return {str(item["user_id"]): str(item["full_name"]) for item in items}


def _compose(document: dict[str, Any], names: dict[str, str] | None) -> dict[str, Any]:
    items = [
        {
            "registration_id": item["registration_id"],
            "athlete_id": item["athlete_id"],
            "display_name": names.get(item["athlete_id"]) if names is not None else None,
            "status": item["status"],
            "registered_at": item["registered_at"],
            "applied_fee": item["applied_fee"],
        }
        for item in document["items"]
    ]
    return {
        "items": items,
        "total": document["total"],
        "by_status": document["by_status"],
        "next_cursor": document["next_cursor"],
        "sources": [
            {"source": USERS_SOURCE, "status": "OK" if names is not None else "UNAVAILABLE"}
        ],
    }


def build_event_attendees_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    permissions: ActorPermissions,
    events: EventsManagementClient,
    users: UsersManagementClient,
    channel: str,
) -> APIRouter:
    router = APIRouter(tags=["events"])

    @router.get(
        "/v1/me/events/{event_id}/registrations",
        response_model=EventAttendeesResponse,
        summary="Inscritos de un evento propio (HU022)",
        description="Orden `registered_at`, `registration_id` ascendente con cursor keyset. "
        "Identidad mínima: `display_name` desde Usuarios, sin correo ni perfil. Si Usuarios no "
        "responde, `display_name` es `null` y `sources[users]` es `UNAVAILABLE`.",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required`: no es `organizer` con "
                "`events.attendees.view`.",
            },
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": "`event_not_found`: el evento no existe o es de otro organizador.",
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
        status: Annotated[
            str | None, Query(description="`REGISTERED` (por defecto) o `ALL`.", max_length=32)
        ] = None,
        limit: Annotated[str | None, Query(description="1 a 100; por defecto 20.")] = None,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        response = _attendees(event_id, status, limit, cursor, authorization)
        response.headers["Cache-Control"] = "no-store"
        return response

    def _attendees(
        event_id: UUID,
        status: str | None,
        limit: str | None,
        cursor: str | None,
        authorization: str | None,
    ) -> Response:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        actor = session.actor_type
        if actor != ORGANIZER or ATTENDEES_PERMISSION not in permissions.for_actor(actor, channel):
            return _permission_required()
        params = {
            name: value
            for name, value in (("status", status), ("limit", limit), ("cursor", cursor))
            if value is not None
        }
        try:
            upstream = events.list_attendees(session.token, event_id, params)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        if upstream.status_code != HTTPStatus.OK:
            return _passthrough(
                upstream,
                {HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND, HTTPStatus.UNPROCESSABLE_ENTITY},
                source=SOURCE,
            )
        try:
            document: dict[str, Any] = upstream.json()
            athlete_ids = list(dict.fromkeys(item["athlete_id"] for item in document["items"]))
        except (ValueError, KeyError, TypeError):
            return _upstream_failure(UpstreamUnavailable())
        return JSONResponse(
            status_code=HTTPStatus.OK, content=_compose(document, _names(users, athlete_ids))
        )

    return router
