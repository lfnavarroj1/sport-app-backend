"""HU041 en el canal móvil: detalle de evento, inscripción y «mis inscripciones» (ADR-032).

El BFF autentica (JWKS), filtra por actor `athlete` y reenvía la clave de idempotencia, los
parámetros y la correlación sin reinterpretarlos; el deportista sale del token y
`athlete_id` nunca se acepta del cliente. Las reglas (publicado, inicio futuro, unicidad,
cupo, idempotencia) y sus errores los decide `events_management` y se entregan sin alterar.
La app solo se inscribe con conectividad y reintenta únicamente ante red, `502`, `503` o
`504` con la misma clave.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bff_mobile.clients.events_management import EventsManagementClient
from bff_mobile.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.errors import ErrorResponse, error_response
from sportapp_platform.tokens import InvalidAccessToken, JwksUnavailable, VerifiedAccessToken

ATHLETE = "athlete"
BEARER_PREFIX = "bearer "
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"

_logger = logging.getLogger("sportapp.bff")


class Location(BaseModel):
    name: str
    city: str
    country_code: str


class Fee(BaseModel):
    amount: Annotated[str, Field(description="Cadena decimal con dos decimales.")]
    currency: str


class EventFee(Fee):
    """Tarifa informativa del evento (ADR-040): `{amount, currency}`; `0.00` es gratuito."""


class MyRegistration(BaseModel):
    registration_id: UUID
    status: Literal["REGISTERED"]
    registered_at: datetime


class EventDetailResponse(BaseModel):
    event_id: UUID
    name: str
    description: str
    discipline: str
    location: Location
    starts_at: datetime
    ends_at: datetime
    time_zone: str
    capacity: int
    status: Literal["PUBLISHED"]
    version: int
    fee: Annotated[EventFee | None, Field(description="Informativa; `null` sin tarifa (HU025).")]
    my_registration: MyRegistration | None


class RegistrationEvent(BaseModel):
    name: str
    starts_at: datetime
    ends_at: datetime
    time_zone: str
    location: Location


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


def _unauthenticated() -> Response:
    response = error_response(
        HTTPStatus.UNAUTHORIZED, InvalidAccessToken.code, "La sesión no es válida."
    )
    response.headers["WWW-Authenticate"] = "Bearer"
    return response


def _failure(error: UpstreamTimeout | UpstreamUnavailable) -> Response:
    if isinstance(error, UpstreamTimeout):
        return error_response(
            HTTPStatus.GATEWAY_TIMEOUT, error.code, "Un servicio interno no respondió."
        )
    return error_response(
        HTTPStatus.SERVICE_UNAVAILABLE, error.code, "Un servicio interno no está disponible."
    )


def _passthrough(upstream: httpx.Response, allowed: set[int]) -> Response:
    if upstream.status_code not in allowed:
        _logger.warning(
            "bff.upstream_unexpected_status",
            extra={"fields": {"upstream": "events-management", "status": upstream.status_code}},
        )
        return _failure(UpstreamUnavailable())
    headers = {
        name: upstream.headers[name]
        for name in ("Location", REPLAYED_HEADER, "Cache-Control")
        if name in upstream.headers
    }
    return JSONResponse(status_code=upstream.status_code, content=upstream.json(), headers=headers)


_COMMON: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNAUTHORIZED.value: {
        "model": ErrorResponse,
        "description": "`invalid_access_token`: falta el token o no es válido.",
    },
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required`: solo el deportista.",
    },
    HTTPStatus.SERVICE_UNAVAILABLE.value: {
        "model": ErrorResponse,
        "description": "`upstream_unavailable`.",
    },
    HTTPStatus.GATEWAY_TIMEOUT.value: {
        "model": ErrorResponse,
        "description": "`upstream_timeout`.",
    },
}
_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    HTTPStatus.NOT_FOUND.value: {
        "model": ErrorResponse,
        "description": "`event_not_found`: inexistente o no publicado.",
    }
}
_QUERY_ERRORS: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNPROCESSABLE_ENTITY.value: {
        "model": ErrorResponse,
        "description": "`validation_error`.",
    }
}


def build_event_registrations_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    events: EventsManagementClient,
) -> APIRouter:
    router = APIRouter(tags=["event-registrations"])

    def athlete_token(authorization: str | None) -> str | Response:
        if not authorization or not authorization.lower().startswith(BEARER_PREFIX):
            return _unauthenticated()
        token = authorization[len(BEARER_PREFIX) :].strip()
        try:
            identity = verify_token(token)
        except InvalidAccessToken:
            return _unauthenticated()
        except JwksUnavailable:
            _logger.warning("bff.jwks_unavailable")
            return _failure(UpstreamUnavailable())
        if identity.actor_type != ATHLETE:
            return error_response(
                HTTPStatus.FORBIDDEN,
                "permission_required",
                "Su tipo de usuario no puede realizar esta operación.",
            )
        return token

    def relay(send: Callable[[], httpx.Response], allowed: set[int]) -> Response:
        try:
            upstream = send()
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _failure(error)
        return _passthrough(upstream, allowed)

    @router.get(
        "/v1/events/{event_id}",
        response_model=EventDetailResponse,
        summary="Detalle de un evento publicado con mi inscripción (HU041)",
        responses={**_COMMON, **_NOT_FOUND},
    )
    def event_detail(
        event_id: UUID, authorization: Annotated[str | None, Header()] = None
    ) -> Response:
        token = athlete_token(authorization)
        if isinstance(token, Response):
            return token
        return relay(
            lambda: events.event_detail(token, event_id),
            {HTTPStatus.OK, HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND},
        )

    @router.post(
        "/v1/events/{event_id}/registrations",
        status_code=HTTPStatus.CREATED,
        response_model=RegistrationResponse,
        summary="Inscribirme a un evento publicado (HU041)",
        responses={
            **_COMMON,
            **_NOT_FOUND,
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
                "description": "`event_registration_exists` (con `registration_id` en `details`), "
                "`event_full`, `event_registration_closed` o `idempotency_key_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `Idempotency-Key` ausente o inválida.",
            },
        },
    )
    def register(
        event_id: UUID,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = athlete_token(authorization)
        if isinstance(token, Response):
            return token
        return relay(
            lambda: events.register(token, event_id, idempotency_key),
            {
                HTTPStatus.CREATED,
                HTTPStatus.FORBIDDEN,
                HTTPStatus.NOT_FOUND,
                HTTPStatus.CONFLICT,
                HTTPStatus.UNPROCESSABLE_ENTITY,
            },
        )

    @router.get(
        "/v1/me/event-registrations",
        response_model=RegistrationsResponse,
        summary="Mis inscripciones a eventos (HU041)",
        responses={**_COMMON, **_QUERY_ERRORS},
    )
    def my_registrations(
        *,
        status: Annotated[str | None, Query()] = None,
        limit: Annotated[str | None, Query(description="1 a 100; por defecto 20.")] = None,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = athlete_token(authorization)
        if isinstance(token, Response):
            return token
        params = {
            name: value
            for name, value in (("status", status), ("limit", limit), ("cursor", cursor))
            if value is not None
        }
        return relay(
            lambda: events.my_registrations(token, params),
            {HTTPStatus.OK, HTTPStatus.FORBIDDEN, HTTPStatus.UNPROCESSABLE_ENTITY},
        )

    return router
