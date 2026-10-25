"""HU040 en el canal móvil: sesiones virtuales con entrenadores (ADR-046).

`POST /v1/me/appointments`, `DELETE /v1/me/appointments/{appointment_id}` y
`GET /v1/me/appointments`. El BFF autentica (JWKS) y filtra el actor (`athlete`); para
reservar consulta además la capacidad `services.virtual_sessions.schedule` ante Afiliación
(sin ella `403 capability_required`; sin respuesta de Afiliación, también por timeout,
`503 upstream_unavailable`: nunca se concede por suposición). Cancelar y listar no exigen la
capacidad. Cuerpo, `Idempotency-Key` y parámetros se reenvían sin reinterpretarlos:
`complementary_services` vuelve a verificar token, actor y capacidad y decide la elegibilidad,
el cupo, la unicidad, la ventana de cancelación y los `422`. La reserva se confirma solo en
línea (no se encola). Reservar no cobra y `meeting_url` es siempre `null`. Los instantes llegan
en UTC (`Z`) con la zona del intervalo; el cliente los convierte.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Body, Header, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from bff_mobile.clients.complementary_services import ComplementaryServicesClient
from bff_mobile.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.errors import ErrorResponse, error_response
from sportapp_platform.tokens import InvalidAccessToken, JwksUnavailable, VerifiedAccessToken

ATHLETE = "athlete"
BEARER_PREFIX = "bearer "
VIRTUAL_SESSIONS_CAPABILITY = "services.virtual_sessions.schedule"
CAPABILITY_REQUIRED = "capability_required"
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
SOURCE = "complementary-services"
# Mismo patrón documentado que el servicio (ADR-027); el BFF no lo aplica.
AMOUNT_PATTERN = r"^\d{1,9}(\.\d{1,2})?$"
# Cabeceras del servicio que el cliente necesita tal como llegan.
RELAYED_HEADERS = ("Location", REPLAYED_HEADER, "Cache-Control", "WWW-Authenticate")

_logger = logging.getLogger("sportapp.bff")


class Category(StrEnum):
    SPORTS_MEDICINE = "SPORTS_MEDICINE"
    COACHING = "COACHING"
    NUTRITION = "NUTRITION"
    TRANSPORT = "TRANSPORT"
    ACCOMPANIMENT = "ACCOMPANIMENT"


class Modality(StrEnum):
    IN_PERSON = "IN_PERSON"
    VIRTUAL = "VIRTUAL"
    HYBRID = "HYBRID"


class BillingUnit(StrEnum):
    SERVICE = "SERVICE"
    SESSION = "SESSION"
    HOUR = "HOUR"
    DAY = "DAY"


class ServicePrice(BaseModel):
    """Precio público de referencia en COP, impuestos aplicables incluidos; no es un cobro."""

    model_config = ConfigDict(extra="forbid")

    amount: Annotated[
        str,
        Field(
            pattern=AMOUNT_PATTERN,
            description="Cadena decimal de 0.00 a 999999999.99, máximo dos decimales.",
            examples=["120000.00"],
        ),
    ]
    currency: Literal["COP"]
    billing_unit: BillingUnit


class BookAppointmentRequest(BaseModel):
    """Esquema documentado del cuerpo; la validación la hace el servicio."""

    model_config = ConfigDict(extra="forbid")

    service_id: Annotated[
        UUID,
        Field(description="Servicio `PUBLISHED` de `COACHING` con modalidad `VIRTUAL`/`HYBRID`."),
    ]
    availability_id: Annotated[
        UUID,
        Field(
            description="Intervalo del servicio (de `GET /v1/services/availability`) que "
            "empieza por lo menos 1 h después."
        ),
    ]


class AssociatedServiceSummary(BaseModel):
    """Resumen del servicio; la tarifa es informativa: reservar no cobra."""

    name: str
    category: Category
    modality: Modality
    fee: ServicePrice


class AppointmentResponse(BaseModel):
    appointment_id: UUID
    athlete_id: UUID
    provider_id: Annotated[UUID, Field(description="Profesional dueño del servicio.")]
    service_id: UUID
    availability_id: UUID
    service: Annotated[
        AssociatedServiceSummary,
        Field(description="Resumen del servicio; la tarifa es informativa: reservar no cobra."),
    ]
    starts_at: Annotated[datetime, Field(description="Inicio del intervalo en UTC (`Z`).")]
    ends_at: datetime
    time_zone: Annotated[
        str, Field(description="Zona IANA declarada por el profesional; el cliente convierte.")
    ]
    status: Literal["BOOKED", "CANCELLED_BY_ATHLETE"]
    meeting_url: Annotated[
        None,
        Field(
            description="Siempre `null` en el MVP: no hay videollamada; el profesional "
            "coordina el enlace."
        ),
    ]
    booked_at: datetime
    cancelled_at: Annotated[
        datetime | None, Field(description="Instante de la cancelación; `null` si sigue reservada.")
    ]
    version: int


class AppointmentsResponse(BaseModel):
    items: list[AppointmentResponse]
    next_cursor: Annotated[
        str | None, Field(description="Cursor opaco de la página siguiente; `null` al final.")
    ]


def _request_schema() -> dict[str, Any]:
    return BookAppointmentRequest.model_json_schema(ref_template="#/components/schemas/{model}")


def _unauthenticated() -> Response:
    response = error_response(
        HTTPStatus.UNAUTHORIZED, InvalidAccessToken.code, "La sesión no es válida."
    )
    response.headers["WWW-Authenticate"] = "Bearer"
    return response


def _permission_required() -> Response:
    return error_response(
        HTTPStatus.FORBIDDEN,
        "permission_required",
        "Su tipo de usuario no puede realizar esta operación.",
    )


def _failure(error: UpstreamTimeout | UpstreamUnavailable) -> Response:
    if isinstance(error, UpstreamTimeout):
        return error_response(
            HTTPStatus.GATEWAY_TIMEOUT, error.code, "Un servicio interno no respondió."
        )
    return error_response(
        HTTPStatus.SERVICE_UNAVAILABLE, error.code, "Un servicio interno no está disponible."
    )


def _unexpected(status: int) -> Response:
    _logger.warning(
        "bff.upstream_unexpected_status",
        extra={"fields": {"upstream": SOURCE, "status": status}},
    )
    return _failure(UpstreamUnavailable())


def _passthrough(upstream: httpx.Response, allowed: set[int]) -> Response:
    """Respuesta del servicio sin alterar; un estado no documentado es `503`."""
    if upstream.status_code not in allowed:
        return _unexpected(upstream.status_code)
    try:
        content = upstream.json()
    except ValueError:
        return _unexpected(upstream.status_code)
    headers = {name: upstream.headers[name] for name in RELAYED_HEADERS if name in upstream.headers}
    return JSONResponse(status_code=upstream.status_code, content=content, headers=headers)


def _private(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def athlete_session(
    verify_token: Callable[[str], VerifiedAccessToken], authorization: str | None
) -> tuple[str, UUID] | Response:
    """Borde: token verificado y actor `athlete`; devuelve el token y el deportista."""
    if not authorization or not authorization.lower().startswith(BEARER_PREFIX):
        return _unauthenticated()
    token = authorization[len(BEARER_PREFIX) :].strip()
    try:
        identity = verify_token(token)
        user_id = UUID(identity.subject)
    except (InvalidAccessToken, ValueError):
        return _unauthenticated()
    except JwksUnavailable:
        _logger.warning("bff.jwks_unavailable")
        return _failure(UpstreamUnavailable())
    if identity.actor_type != ATHLETE:
        return _permission_required()
    return token, user_id


def relay(send: Callable[[], httpx.Response], allowed: set[int]) -> Response:
    try:
        upstream = send()
    except (UpstreamTimeout, UpstreamUnavailable) as error:
        return _failure(error)
    return _passthrough(upstream, allowed)


UNAUTHORIZED: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNAUTHORIZED.value: {
        "model": ErrorResponse,
        "description": "`invalid_access_token`: falta el token o no es válido.",
    }
}
ATHLETE_ONLY: dict[int | str, dict[str, Any]] = {
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required`: solo el deportista.",
    }
}
UPSTREAM: dict[int | str, dict[str, Any]] = {
    HTTPStatus.SERVICE_UNAVAILABLE.value: {
        "model": ErrorResponse,
        "description": "`upstream_unavailable`: un servicio interno no está disponible o no se "
        "pudo verificar la identidad o la capacidad ante Afiliación (también por timeout).",
    },
    HTTPStatus.GATEWAY_TIMEOUT.value: {
        "model": ErrorResponse,
        "description": "`upstream_timeout`: Servicios complementarios no respondió.",
    },
}
_NO_STORE = {"Cache-Control": {"description": "`no-store`.", "schema": {"type": "string"}}}


def build_appointments_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    athlete_snapshot: Callable[[UUID], dict[str, Any]],
    services: ComplementaryServicesClient,
) -> APIRouter:
    router = APIRouter(tags=["appointments"])

    def scheduling_athlete(authorization: str | None) -> str | Response:
        """Deportista con `services.virtual_sessions.schedule` vigente según Afiliación."""
        caller = athlete_session(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        token, user_id = caller
        try:
            snapshot = athlete_snapshot(user_id)
        except (UpstreamTimeout, UpstreamUnavailable, ValueError) as error:
            # ADR-046/ADR-038: la capacidad no evaluable es `503`, también por timeout; nunca
            # se concede por suposición.
            _logger.warning(
                "bff.capabilities_unavailable",
                extra={"fields": {"timeout": isinstance(error, UpstreamTimeout)}},
            )
            return _failure(UpstreamUnavailable())
        granted = snapshot.get("capabilities") or []
        if (
            snapshot.get("subscription_status") != "ACTIVE"
            or VIRTUAL_SESSIONS_CAPABILITY not in granted
        ):
            return error_response(
                HTTPStatus.FORBIDDEN, CAPABILITY_REQUIRED, "Su plan no incluye esta funcionalidad."
            )
        return token

    def plain_athlete(authorization: str | None) -> str | Response:
        caller = athlete_session(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        return caller[0]

    @router.post(
        "/v1/me/appointments",
        status_code=HTTPStatus.CREATED,
        response_model=AppointmentResponse,
        summary="Reservar una sesión virtual con un entrenador (HU040, ADR-046)",
        description="Confirma la cita solo en línea (no se encola): el servicio bloquea el "
        "intervalo y confirma la cita, el cupo y la clave en una transacción. No cobra ni crea "
        "videollamada. Reintentar con la misma `Idempotency-Key` devuelve la misma cita.",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": _request_schema()}},
            }
        },
        responses={
            **UNAUTHORIZED,
            **UPSTREAM,
            HTTPStatus.CREATED.value: {
                "headers": {
                    "Location": {
                        "description": "`/v1/me/appointments/{appointment_id}`.",
                        "schema": {"type": "string"},
                    },
                    REPLAYED_HEADER: {
                        "description": "`true` cuando se devuelve el resultado de un reintento.",
                        "schema": {"type": "string"},
                    },
                }
            },
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required`: solo el deportista; "
                "`capability_required`: su plan no incluye "
                "`services.virtual_sessions.schedule`.",
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`availability_full`: el intervalo no tiene cupo; "
                '`appointment_exists` con `details: [{field: "appointment_id", issue: '
                "<identificador de la cita activa>}]`; `idempotency_key_reused`: la clave se "
                "usó con otro servicio o intervalo.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`service_not_bookable`: el servicio no existe, no está "
                "publicado o no es `COACHING` `VIRTUAL`/`HYBRID`; `availability_not_bookable`: "
                "el intervalo no es del servicio o empieza en menos de 1 h; `validation_error` "
                "con `details[{field, issue}]`: `service_id`/`availability_id`, campos de más, "
                "cuerpo ausente o `Idempotency-Key` ausente o que no es un UUID.",
            },
        },
    )
    def book(
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[
            str | None,
            Header(
                alias=IDEMPOTENCY_HEADER, description="UUID; obligatorio (lo valida el servicio)."
            ),
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = scheduling_athlete(authorization)
        if isinstance(token, Response):
            return token
        return relay(
            lambda: services.book_appointment(token, body, idempotency_key),
            {
                HTTPStatus.CREATED,
                HTTPStatus.UNAUTHORIZED,
                HTTPStatus.FORBIDDEN,
                HTTPStatus.CONFLICT,
                HTTPStatus.UNPROCESSABLE_ENTITY,
                HTTPStatus.SERVICE_UNAVAILABLE,
            },
        )

    @router.delete(
        "/v1/me/appointments/{appointment_id}",
        response_model=AppointmentResponse,
        summary="Cancelar una cita propia (HU040, ADR-046)",
        description="Con 24 h o más de antelación la cita pasa a `CANCELLED_BY_ATHLETE` y el "
        "intervalo recupera el cupo; repetirlo devuelve la cita sin cambios. No hay "
        "reprogramación: cancelar y reservar otro intervalo. No exige capacidad.",
        responses={
            **UNAUTHORIZED,
            **ATHLETE_ONLY,
            **UPSTREAM,
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": "`appointment_not_found`: no existe o es de otro deportista.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`cancellation_window_closed`: faltan menos de 24 h; "
                "`validation_error`: `path.appointment_id` no es un UUID.",
            },
        },
    )
    def cancel(
        appointment_id: UUID,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = plain_athlete(authorization)
        if isinstance(token, Response):
            return token
        return relay(
            lambda: services.cancel_appointment(token, appointment_id),
            {
                HTTPStatus.OK,
                HTTPStatus.UNAUTHORIZED,
                HTTPStatus.FORBIDDEN,
                HTTPStatus.NOT_FOUND,
                HTTPStatus.UNPROCESSABLE_ENTITY,
                HTTPStatus.SERVICE_UNAVAILABLE,
            },
        )

    @router.get(
        "/v1/me/appointments",
        response_model=AppointmentsResponse,
        summary="Mis sesiones virtuales (HU040, ADR-046)",
        description="Ordenadas por `starts_at` y `appointment_id` ascendentes con cursor keyset; "
        "incluye las canceladas salvo que se filtre por `status`. No exige capacidad.",
        responses={
            **UNAUTHORIZED,
            **ATHLETE_ONLY,
            **UPSTREAM,
            HTTPStatus.OK.value: {"headers": _NO_STORE},
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `query.from`/`query.to` (`required` si "
                "llega solo uno, `unknown_value`), `query.to` (`ends_before_starts`), "
                "`query.status` o `query.cursor` (`unknown_value`), `query.limit` "
                "(`unknown_value` u `out_of_range`).",
            },
        },
    )
    def my_appointments(
        *,
        starts_from: Annotated[
            str | None,
            Query(alias="from", description="Instante ISO 8601 con desfase; junto con `to`."),
        ] = None,
        starts_before: Annotated[
            str | None,
            Query(alias="to", description="Instante ISO 8601 con desfase, posterior a `from`."),
        ] = None,
        status: Annotated[
            str | None,
            Query(description="`BOOKED` o `CANCELLED_BY_ATHLETE`; sin valor, todas."),
        ] = None,
        limit: Annotated[
            str | None, Query(description="Entero de 1 a 100; 20 por defecto.")
        ] = None,
        cursor: Annotated[str | None, Query(description="`next_cursor` anterior.")] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = plain_athlete(authorization)
        if isinstance(token, Response):
            return token
        # Solo los presentes y tal como llegan: el servicio valida y responde el `422`.
        pairs = (
            ("from", starts_from),
            ("to", starts_before),
            ("status", status),
            ("limit", limit),
            ("cursor", cursor),
        )
        params = {name: value for name, value in pairs if value is not None}
        return _private(
            relay(
                lambda: services.list_my_appointments(token, params),
                {
                    HTTPStatus.OK,
                    HTTPStatus.UNAUTHORIZED,
                    HTTPStatus.FORBIDDEN,
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    HTTPStatus.SERVICE_UNAVAILABLE,
                },
            )
        )

    return router
