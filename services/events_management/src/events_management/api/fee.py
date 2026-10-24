"""HU025: tarifa informativa de un evento propio (ADR-040).

- `PUT /v1/events/{event_id}/fee` con `{amount, currency, expected_version}` e
  `Idempotency-Key`: asigna la tarifa (`0.00` = gratuito).
- `DELETE /v1/events/{event_id}/fee?expected_version=N` con `Idempotency-Key`: la retira
  (`fee = null`).
Ambas responden `200` con el agregado propio (versión incrementada y `registration_count`)
sin esperar la proyección; `Idempotent-Replayed: true` al repetir la clave. Solo
`organizer` propietario con `events.pricing.manage`; un evento ajeno o inexistente es
`404 event_not_found`. La tarifa no es un cobro ni cambia las inscripciones existentes.

El cuerpo se lee como JSON sin validación automática para responder `422 validation_error`
con `details[{field, issue}]` del catálogo del servicio (`unsupported_currency`,
`too_many_decimals`, `out_of_range`, `event_already_started`...); su esquema se documenta en
el OpenAPI.
"""

from collections.abc import Callable
from http import HTTPStatus
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Header, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from events_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    correlation_uuid,
    forbidden,
)
from events_management.api.publication import OwnEventResponse
from events_management.application.commands.event_fee import (
    ChangeEventFee,
    ChangeEventFeeCommand,
)
from events_management.application.errors import IdempotencyKeyReused, PermissionRequired
from events_management.domain.sport_events.fee import (
    AMOUNT_MAX,
    CURRENCY,
    FeeChange,
    FeeInput,
    VersionConflict,
    validate_fee,
    validate_removal,
)
from events_management.domain.sport_events.model import EventNotFound
from events_management.domain.validation import InvalidSportEvent, Issue, Violation
from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
BODY_FIELDS = ("amount", "currency", "expected_version")


class EventFeeRequest(BaseModel):
    """Esquema documentado del cuerpo; la validación la hacen el transporte y el dominio."""

    model_config = ConfigDict(extra="forbid")

    amount: Annotated[
        str,
        Field(
            pattern=r"^[0-9]+\.[0-9]{2}$",
            description=f"Cadena decimal con exactamente dos decimales, de `0.00` (gratuito) a "
            f"`{AMOUNT_MAX}`.",
            examples=["85000.00"],
        ),
    ]
    currency: Annotated[str, Field(description="Solo `COP`.", examples=[CURRENCY])]
    expected_version: Annotated[
        int, Field(ge=1, description="Versión vigente del evento que vio el organizador.")
    ]


def _request_schema() -> dict[str, Any]:
    schema = EventFeeRequest.model_json_schema(ref_template="#/components/schemas/{model}")
    schema.pop("$defs", None)
    return schema


def _string(body: dict[str, Any], name: str, violations: list[Violation]) -> str | None:
    value = body.get(name)
    if value is None:
        violations.append(Violation(name, Issue.REQUIRED))
        return None
    if not isinstance(value, str):
        violations.append(Violation(name, Issue.UNKNOWN_VALUE))
        return None
    return value


def _version(value: object, field: str, violations: list[Violation]) -> int | None:
    if value is None:
        violations.append(Violation(field, Issue.REQUIRED))
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        violations.append(Violation(field, Issue.UNKNOWN_VALUE))
        return None
    return value


def parse_fee(body: object) -> FeeInput:
    """Tarifa con los campos de forma válida y las violaciones de transporte encontradas."""
    if not isinstance(body, dict):
        issue = Issue.REQUIRED if body is None else Issue.UNKNOWN_VALUE
        return FeeInput(None, None, None, (Violation(None, issue),))
    violations = [
        Violation(name, Issue.FIELD_NOT_ALLOWED) for name in body if name not in BODY_FIELDS
    ]
    return FeeInput(
        amount=_string(body, "amount", violations),
        currency=_string(body, "currency", violations),
        expected_version=_version(body.get("expected_version"), "expected_version", violations),
        transport_violations=tuple(violations),
    )


def _query_version(raw: str | None, violations: list[Violation]) -> int | None:
    field = "query.expected_version"
    if raw is None or not raw.strip():
        violations.append(Violation(field, Issue.REQUIRED))
        return None
    text = raw.strip()
    if not text.isascii() or not text.isdigit():
        violations.append(Violation(field, Issue.UNKNOWN_VALUE))
        return None
    value = int(text)
    if value < 1:
        violations.append(Violation(field, Issue.OUT_OF_RANGE))
        return None
    return value


def _idempotency_key(raw: str | None, violations: list[Violation]) -> UUID | None:
    if raw is None or not raw.strip():
        violations.append(Violation(IDEMPOTENCY_HEADER, Issue.REQUIRED))
        return None
    try:
        return UUID(raw.strip())
    except ValueError:
        violations.append(Violation(IDEMPOTENCY_HEADER, Issue.UNKNOWN_VALUE))
        return None


def _invalid(violations: list[Violation] | tuple[Violation, ...]) -> Response:
    return error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        InvalidSportEvent.code,
        "La solicitud no es válida.",
        [ErrorDetail(field=item.field, issue=item.issue.value) for item in violations],
    )


_FEE_ERRORS = (
    PermissionRequired,
    EventNotFound,
    InvalidSportEvent,
    VersionConflict,
    IdempotencyKeyReused,
)


def _failure(error: Exception) -> Response:
    if isinstance(error, PermissionRequired):
        return forbidden(error)
    if isinstance(error, EventNotFound):
        return error_response(
            HTTPStatus.NOT_FOUND, error.code, "El evento no existe o no está disponible."
        )
    if isinstance(error, InvalidSportEvent):
        return _invalid(error.violations)
    if isinstance(error, VersionConflict):
        return error_response(
            HTTPStatus.CONFLICT,
            error.code,
            "El evento cambió desde que lo consultó. Vuelva a cargarlo.",
            [ErrorDetail(field="expected_version", issue=str(error.current_version))],
        )
    return error_response(
        HTTPStatus.CONFLICT,
        IdempotencyKeyReused.code,
        "La clave de idempotencia ya se usó con otra solicitud.",
    )


_RESPONSES: dict[int | str, dict[str, Any]] = {
    **AUTH_RESPONSES,
    HTTPStatus.OK.value: {
        "headers": {
            REPLAYED_HEADER: {
                "description": "`true` cuando se devuelve el resultado de un reintento.",
                "schema": {"type": "string"},
            }
        }
    },
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required`: no es `organizer` con `events.pricing.manage`.",
    },
    HTTPStatus.NOT_FOUND.value: {
        "model": ErrorResponse,
        "description": "`event_not_found`: inexistente o de otro organizador.",
    },
    HTTPStatus.CONFLICT.value: {
        "model": ErrorResponse,
        "description": "`version_conflict` (`details[0]` = `{field: expected_version, issue: "
        "<versión vigente>}`) o `idempotency_key_reused`.",
    },
}


def build_fee_router(*, verify_token: TokenVerifier, change_fee: ChangeEventFee) -> APIRouter:
    router = APIRouter(prefix="/v1/events/{event_id}/fee", tags=["events"])

    def execute(
        event_id: UUID,
        authorization: str | None,
        raw_key: str | None,
        validate: Callable[[list[Violation]], FeeChange],
    ) -> Response:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            # Actor y permiso antes que la validación: un actor sin permiso recibe `403`.
            change_fee.authorize(caller)
        except PermissionRequired as error:
            return forbidden(error)
        problems: list[Violation] = []
        key = _idempotency_key(raw_key, problems)
        try:
            change = validate(problems)
        except InvalidSportEvent as error:
            return _invalid(error.violations)
        if key is None:
            return _invalid(problems)
        try:
            result = change_fee(
                caller,
                ChangeEventFeeCommand(
                    event_id=event_id,
                    change=change,
                    idempotency_key=key,
                    correlation_id=correlation_uuid(),
                ),
            )
        except _FEE_ERRORS as error:
            return _failure(error)
        headers = {REPLAYED_HEADER: "true"} if result.replayed else {}
        return JSONResponse(result.document(), headers=headers)

    @router.put(
        "",
        response_model=OwnEventResponse,
        summary="Asignar la tarifa de un evento propio (HU025)",
        description="`DRAFT` o `PUBLISHED` antes de `starts_at`. Incrementa la versión y "
        "devuelve `registration_count`; las inscripciones existentes conservan su "
        "`applied_fee`. No genera cobro, estado de pago ni reembolso.",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": _request_schema()}},
            }
        },
        responses={
            **_RESPONSES,
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` con `details[{field, issue}]`: `amount` "
                "(`required`, `unknown_value` sin dos decimales, `too_many_decimals`, "
                "`out_of_range`), `currency` (`unsupported_currency`), `expected_version`, "
                "campos no permitidos (`field_not_allowed`), `starts_at` "
                "(`event_already_started`) o `Idempotency-Key` ausente o inválida.",
            },
        },
    )
    def put_fee(
        event_id: UUID,
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        raw = parse_fee(body)

        def validate(problems: list[Violation]) -> FeeChange:
            # La clave inválida se informa junto con las reglas de la tarifa.
            return validate_fee(
                FeeInput(
                    raw.amount,
                    raw.currency,
                    raw.expected_version,
                    (*problems, *raw.transport_violations),
                )
            )

        return execute(event_id, authorization, idempotency_key, validate)

    @router.delete(
        "",
        response_model=OwnEventResponse,
        summary="Retirar la tarifa de un evento propio (HU025)",
        description="Deja `fee = null` con las mismas reglas de estado, inicio y versión que "
        "la asignación.",
        responses={
            **_RESPONSES,
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` con `details[{field, issue}]`: "
                "`query.expected_version` (`required`, `unknown_value`, `out_of_range`), "
                "`starts_at` (`event_already_started`) o `Idempotency-Key` ausente o inválida.",
            },
        },
    )
    def delete_fee(
        event_id: UUID,
        expected_version: Annotated[
            str | None,
            Query(description="Versión vigente del evento que vio el organizador (entero ≥ 1)."),
        ] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        def validate(problems: list[Violation]) -> FeeChange:
            version = _query_version(expected_version, problems)
            return validate_removal(version, problems)

        return execute(event_id, authorization, idempotency_key, validate)

    return router
