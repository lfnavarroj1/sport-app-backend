"""HU040 en el canal web: citas de los servicios del profesional (ADR-046 §4).

`GET /v1/me/appointments?from&to&status&limit&cursor`:
1. Filtro de borde: solo `service_provider` (la reserva y la cancelación son del canal móvil).
2. Reenvía la consulta a `complementary-services` 0.6.0, que vuelve a verificar identidad y
   actor y devuelve solo las citas de los servicios del profesional; sus `403` y `422` se
   entregan sin alterar.
3. Compone `athlete_display_name` con `POST /v1/users/directory-lookup` (solo `full_name`,
   ADR-039). Si Usuarios no responde, la lista se entrega igual con `null` y
   `sources[users] = UNAVAILABLE`. Nunca correo, perfil ni datos de salud; los nombres no
   se registran en logs.
La misma consulta es la fuente `OWN_APPOINTMENT` del calendario integrado (HU018).
`meeting_url` es siempre `null`: el profesional coordina el enlace (sin videollamada).
"""

import logging
from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bff_web.api.service_associations import AssociatedServiceSummary
from bff_web.api.services import SOURCE
from bff_web.api.training import (
    _UNAUTHORIZED,
    _UPSTREAM,
    _passthrough,
    _permission_required,
    _upstream_failure,
    authenticate_session,
)
from bff_web.clients.complementary_services import ComplementaryServicesClient
from bff_web.clients.users_management import (
    UpstreamTimeout,
    UpstreamUnavailable,
    UsersManagementClient,
)
from sportapp_platform.errors import ErrorResponse
from sportapp_platform.tokens import VerifiedAccessToken

SERVICE_PROVIDER = "service_provider"
USERS_SOURCE = "users"

_logger = logging.getLogger("sportapp.bff")


class ProviderAppointmentItem(BaseModel):
    appointment_id: UUID
    athlete_id: UUID
    athlete_display_name: Annotated[
        str | None,
        Field(description="Nombre visible (`full_name`); `null` si Usuarios no lo entregó."),
    ]
    provider_id: UUID
    service_id: UUID
    availability_id: UUID
    service: AssociatedServiceSummary
    starts_at: Annotated[datetime, Field(description="UTC (`Z`).")]
    ends_at: datetime
    time_zone: Annotated[str, Field(description="Zona IANA declarada por el profesional.")]
    status: Literal["BOOKED", "CANCELLED_BY_ATHLETE"]
    meeting_url: Annotated[
        None, Field(description="Siempre `null` en el MVP: el profesional coordina el enlace.")
    ]
    booked_at: datetime
    cancelled_at: datetime | None
    version: int


class AppointmentsSource(BaseModel):
    source: Literal["users"]
    status: Literal["OK", "UNAVAILABLE"]


class ProviderAppointmentsResponse(BaseModel):
    items: list[ProviderAppointmentItem]
    next_cursor: str | None
    sources: Annotated[
        list[AppointmentsSource],
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
            **item,
            "athlete_display_name": (
                names.get(str(item["athlete_id"])) if names is not None else None
            ),
        }
        for item in document["items"]
    ]
    return {
        "items": items,
        "next_cursor": document["next_cursor"],
        "sources": [
            {"source": USERS_SOURCE, "status": "OK" if names is not None else "UNAVAILABLE"}
        ],
    }


def build_appointments_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    services: ComplementaryServicesClient,
    users: UsersManagementClient,
) -> APIRouter:
    router = APIRouter(tags=["appointments"])

    @router.get(
        "/v1/me/appointments",
        response_model=ProviderAppointmentsResponse,
        summary="Citas de mis servicios (HU040, ADR-046)",
        description="Orden `starts_at`, `appointment_id` ascendente con cursor keyset; incluye "
        "las canceladas salvo que se filtre por `status`. Identidad mínima del deportista: "
        "`athlete_display_name` desde Usuarios, sin correo ni perfil. Si Usuarios no responde, "
        "`athlete_display_name` es `null` y `sources[users]` es `UNAVAILABLE`.",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required`: solo el profesional (`service_provider`).",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` del servicio: `query.from`/`query.to` "
                "(`required` si llega solo uno, `unknown_value`, `ends_before_starts`), "
                "`query.status`, `query.cursor` o `query.limit`.",
            },
        },
    )
    def provider_appointments(
        *,
        raw_from: Annotated[
            str | None,
            Query(alias="from", description="Instante ISO 8601 con desfase; junto con `to`."),
        ] = None,
        raw_to: Annotated[
            str | None, Query(alias="to", description="Instante ISO 8601 con desfase.")
        ] = None,
        status: Annotated[
            str | None, Query(description="`BOOKED` o `CANCELLED_BY_ATHLETE`; sin valor, todas.")
        ] = None,
        limit: Annotated[str | None, Query(description="1 a 100; por defecto 20.")] = None,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        # Solo los presentes y tal como llegan: el servicio valida y responde el `422`.
        pairs = (
            ("from", raw_from),
            ("to", raw_to),
            ("status", status),
            ("limit", limit),
            ("cursor", cursor),
        )
        params = {name: value for name, value in pairs if value is not None}
        response = _appointments(params, authorization)
        response.headers["Cache-Control"] = "no-store"
        return response

    def _appointments(params: dict[str, str], authorization: str | None) -> Response:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        if session.actor_type != SERVICE_PROVIDER:
            return _permission_required()
        try:
            upstream = services.list_my_appointments(session.token, params)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        if upstream.status_code != HTTPStatus.OK:
            return _passthrough(
                upstream,
                {
                    HTTPStatus.UNAUTHORIZED,
                    HTTPStatus.FORBIDDEN,
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    HTTPStatus.SERVICE_UNAVAILABLE,
                },
                source=SOURCE,
            )
        try:
            document: dict[str, Any] = upstream.json()
            athlete_ids = list(dict.fromkeys(str(i["athlete_id"]) for i in document["items"]))
        except (ValueError, KeyError, TypeError):
            return _upstream_failure(UpstreamUnavailable())
        return JSONResponse(
            status_code=HTTPStatus.OK, content=_compose(document, _names(users, athlete_ids))
        )

    return router
