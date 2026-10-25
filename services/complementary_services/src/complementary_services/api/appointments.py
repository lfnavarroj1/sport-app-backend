"""HU040: sesiones virtuales con entrenadores (ADR-046).

- `POST /v1/me/appointments` con `{service_id, availability_id}` e `Idempotency-Key`: `201`
  con la cita `BOOKED`; `Idempotent-Replayed: true` en los reintentos. Solo `athlete` con
  `services.virtual_sessions.schedule` (verificada ante Afiliación); el actor y la capacidad
  se informan antes que una solicitud mal formada.
- `DELETE /v1/me/appointments/{appointment_id}`: `200` con la cita cancelada (también al
  repetirlo); `422 cancellation_window_closed` con menos de 24 h. Solo `athlete`.
- `GET /v1/me/appointments?from&to&status&limit&cursor`: del deportista o de los servicios
  del profesional (fuente `OWN_APPOINTMENT` de HU018).
Reservar no cobra; `meeting_url` es siempre `null` (sin videollamada en el MVP). Instantes
en UTC (`Z`) con la zona del intervalo: el frontend convierte.
"""

import logging
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from complementary_services.api.associations import AssociatedServiceSummary
from complementary_services.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    correlation_uuid,
    forbidden,
)
from complementary_services.api.services import (
    IDEMPOTENCY_HEADER,
    REPLAYED_HEADER,
    parse_idempotency_key,
    validation_failure,
)
from complementary_services.application.commands.appointments import (
    BookAppointment,
    BookAppointmentCommand,
    CancelAppointment,
)
from complementary_services.application.errors import (
    AppointmentExists,
    AppointmentNotFound,
    AvailabilityFull,
    AvailabilityNotBookable,
    CancellationWindowClosed,
    CapabilitiesTimeout,
    CapabilitiesUnavailable,
    CapabilityRequired,
    IdempotencyKeyReused,
    PermissionRequired,
    ServiceNotBookable,
)
from complementary_services.application.ports import AppointmentView
from complementary_services.application.queries.my_appointments import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    ListMyAppointments,
    MyAppointmentsFilters,
)
from complementary_services.application.queries.my_services import InvalidServicesQuery
from complementary_services.domain.validation import Issue, Violation
from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response

NO_STORE = "no-store"
BODY_FIELDS = ("service_id", "availability_id")

_logger = logging.getLogger("sportapp.services")


class BookAppointmentRequest(BaseModel):
    """Esquema documentado del cuerpo; la validación la hace el transporte."""

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
    time_zone: Annotated[str, Field(description="Zona IANA declarada por el profesional.")]
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

    @classmethod
    def from_view(cls, view: AppointmentView) -> "AppointmentResponse":
        return cls.model_validate(view.to_document())


class AppointmentsResponse(BaseModel):
    items: list[AppointmentResponse]
    next_cursor: Annotated[
        str | None, Field(description="Cursor opaco de la página siguiente; `null` al final.")
    ]


def _request_schema() -> dict[str, Any]:
    return BookAppointmentRequest.model_json_schema(ref_template="#/components/schemas/{model}")


def _uuid_field(body: dict[str, Any], name: str, violations: list[Violation]) -> UUID | None:
    raw = body.get(name)
    if raw is None:
        violations.append(Violation(name, Issue.REQUIRED))
        return None
    if not isinstance(raw, str):
        violations.append(Violation(name, Issue.UNKNOWN_VALUE))
        return None
    try:
        return UUID(raw)
    except ValueError:
        violations.append(Violation(name, Issue.UNKNOWN_VALUE))
        return None


def parse_booking(body: object) -> tuple[tuple[UUID, UUID] | None, list[Violation]]:
    """`service_id` y `availability_id` con los errores de forma del catálogo de ADR-027."""
    if not isinstance(body, dict):
        issue = Issue.REQUIRED if body is None else Issue.UNKNOWN_VALUE
        return None, [Violation(None, issue)]
    violations = [
        Violation(name, Issue.FIELD_NOT_ALLOWED) for name in body if name not in BODY_FIELDS
    ]
    service_id = _uuid_field(body, "service_id", violations)
    availability_id = _uuid_field(body, "availability_id", violations)
    if violations or service_id is None or availability_id is None:
        return None, violations
    return (service_id, availability_id), violations


def _limit(raw: str | None) -> int | None:
    """`limit` entero; `None` si no es un entero (el rango lo valida la consulta)."""
    if raw is None:
        return DEFAULT_LIMIT
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _query_failure(field: str, issue: Issue) -> Response:
    return validation_failure([Violation(f"query.{field}", issue)])


def _access_failure(error: Exception) -> Response:
    if isinstance(error, PermissionRequired):
        return forbidden(error)
    if isinstance(error, CapabilityRequired):
        return error_response(
            HTTPStatus.FORBIDDEN, error.code, "Su plan no incluye esta funcionalidad."
        )
    _logger.warning(
        "services.capabilities_unavailable",
        extra={"fields": {"timeout": isinstance(error, CapabilitiesTimeout)}},
    )
    return error_response(
        HTTPStatus.SERVICE_UNAVAILABLE,
        CapabilitiesUnavailable.code,
        "No fue posible verificar el plan. Intente de nuevo.",
    )


_ACCESS_ERRORS = (PermissionRequired, CapabilityRequired, CapabilitiesUnavailable)
_BOOKING_ERRORS = (
    ServiceNotBookable,
    AvailabilityNotBookable,
    AvailabilityFull,
    AppointmentExists,
    IdempotencyKeyReused,
)


def _booking_failure(error: Exception) -> Response:
    if isinstance(error, ServiceNotBookable):
        return error_response(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            error.code,
            "El servicio no admite sesiones virtuales con reserva.",
        )
    if isinstance(error, AvailabilityNotBookable):
        return error_response(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            error.code,
            "El horario no se puede reservar: debe empezar al menos una hora después.",
        )
    if isinstance(error, AvailabilityFull):
        return error_response(HTTPStatus.CONFLICT, error.code, "El horario ya no tiene cupo.")
    if isinstance(error, AppointmentExists):
        return error_response(
            HTTPStatus.CONFLICT,
            error.code,
            "Ya tiene una cita reservada en este horario.",
            [ErrorDetail(field="appointment_id", issue=str(error.appointment_id))],
        )
    return error_response(
        HTTPStatus.CONFLICT,
        IdempotencyKeyReused.code,
        "La clave de idempotencia ya se usó con otra solicitud.",
    )


_RESPONSES: dict[int | str, dict[str, Any]] = {
    **AUTH_RESPONSES,
    HTTPStatus.SERVICE_UNAVAILABLE.value: {
        "model": ErrorResponse,
        "description": "`upstream_unavailable`: no se pudo verificar la identidad o la "
        "capacidad ante Afiliación (también por timeout).",
    },
}


def build_appointments_router(
    *,
    verify_token: TokenVerifier,
    book_appointment: BookAppointment,
    cancel_appointment: CancelAppointment,
    list_my_appointments: ListMyAppointments,
) -> APIRouter:
    router = APIRouter(prefix="/v1/me/appointments", tags=["appointments"])

    @router.post(
        "",
        status_code=HTTPStatus.CREATED,
        response_model=AppointmentResponse,
        summary="Reservar una sesión virtual con un entrenador (HU040)",
        description="Bloquea el intervalo y confirma la cita, el cupo, la clave y "
        "`sesion.programada.v1` en una transacción. No cobra ni crea videollamada.",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": _request_schema()}},
            }
        },
        responses={
            **_RESPONSES,
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
                "con `details[{field, issue}]`: `service_id`/`availability_id` (`required`, "
                "`unknown_value`), otro campo (`field_not_allowed`), cuerpo ausente o que no "
                "es un objeto (`field: null`), `Idempotency-Key` ausente (`required`) o que no "
                "es un UUID (`unknown_value`).",
            },
        },
    )
    def book(
        response: Response,
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | AppointmentResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            book_appointment.authorize(caller)
        except _ACCESS_ERRORS as error:
            return _access_failure(error)
        target, problems = parse_booking(body)
        key, key_problem = parse_idempotency_key(idempotency_key)
        if key_problem is not None:
            problems.append(key_problem)
        if target is None or key is None:
            return validation_failure(problems)
        service_id, availability_id = target
        try:
            result = book_appointment.execute(
                caller,
                BookAppointmentCommand(
                    service_id=service_id,
                    availability_id=availability_id,
                    idempotency_key=key,
                    correlation_id=correlation_uuid(),
                ),
            )
        except _BOOKING_ERRORS as error:
            return _booking_failure(error)
        appointment_id = result.appointment.appointment.appointment_id
        response.headers["Location"] = f"/v1/me/appointments/{appointment_id}"
        if result.replayed:
            response.headers[REPLAYED_HEADER] = "true"
        return AppointmentResponse.from_view(result.appointment)

    @router.delete(
        "/{appointment_id}",
        response_model=AppointmentResponse,
        summary="Cancelar una cita propia (HU040)",
        description="Con 24 h o más de antelación la cita pasa a `CANCELLED_BY_ATHLETE`, el "
        "intervalo recupera el cupo y se publica `sesion.cancelada.v1`; repetirlo devuelve la "
        "cita sin cambios. No hay reprogramación: cancelar y reservar otro intervalo.",
        responses={
            **_RESPONSES,
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required`: solo el deportista.",
            },
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
    ) -> Response | AppointmentResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            result = cancel_appointment(caller, appointment_id, correlation_uuid())
        except PermissionRequired as error:
            return forbidden(error)
        except AppointmentNotFound as error:
            return error_response(
                HTTPStatus.NOT_FOUND, error.code, "La cita no existe o no le pertenece."
            )
        except CancellationWindowClosed as error:
            return error_response(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                error.code,
                "La cita solo se puede cancelar con 24 horas o más de antelación.",
            )
        return AppointmentResponse.from_view(result.appointment)

    @router.get(
        "",
        response_model=AppointmentsResponse,
        summary="Mis citas (HU040)",
        description="Del deportista, o de los servicios del profesional (fuente "
        "`OWN_APPOINTMENT` del calendario integrado). Ordenadas por `starts_at` y "
        "`appointment_id` ascendentes con cursor keyset; incluye las canceladas salvo que se "
        "filtre por `status`.",
        responses={
            **_RESPONSES,
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required`: solo el deportista o el profesional.",
            },
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
        response: Response,
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
            str | None,
            Query(description=f"Entero de 1 a {MAX_LIMIT}; {DEFAULT_LIMIT} por defecto."),
        ] = None,
        cursor: Annotated[str | None, Query(description="`next_cursor` anterior.")] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | AppointmentsResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        page_size = _limit(limit)
        try:
            # El actor se informa antes que un parámetro mal formado.
            list_my_appointments.authorize(caller)
            if page_size is None:
                return _query_failure("limit", Issue.UNKNOWN_VALUE)
            page = list_my_appointments(
                caller,
                MyAppointmentsFilters(
                    starts_from=starts_from,
                    starts_before=starts_before,
                    status=status,
                    limit=page_size,
                    cursor=cursor,
                ),
            )
        except PermissionRequired as error:
            return forbidden(error)
        except InvalidServicesQuery as error:
            return _query_failure(error.field, error.issue)
        response.headers["Cache-Control"] = NO_STORE
        return AppointmentsResponse(
            items=[AppointmentResponse.from_view(item) for item in page.items],
            next_cursor=page.next_cursor,
        )

    return router
