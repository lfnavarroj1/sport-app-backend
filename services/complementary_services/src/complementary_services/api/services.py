"""HU015: `POST /v1/services` (contrato de ADR-027).

El cuerpo se lee como JSON sin validación automática para responder los errores con
el catálogo de ADR-027 (`field` con índices entre corchetes e `issue` estable); su
esquema se documenta en el OpenAPI. Los instantes se responden siempre en UTC (`Z`):
la conversión a la zona del usuario corresponde al frontend.
"""

from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Header, Response
from pydantic import BaseModel, ConfigDict, Field

from complementary_services.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    correlation_uuid,
    forbidden,
)
from complementary_services.api.payload import parse_offer
from complementary_services.application.commands.create_service import (
    CreateService,
    CreateServiceCommand,
)
from complementary_services.application.errors import IdempotencyKeyReused, PermissionRequired
from complementary_services.application.ports import ServiceView
from complementary_services.domain.availability.intervals import CAPACITY_MAX, CAPACITY_MIN
from complementary_services.domain.catalog.offer import (
    DESCRIPTION_MAX,
    DESCRIPTION_MIN,
    NAME_MAX,
    NAME_MIN,
    BillingUnit,
    Category,
    Modality,
    ServiceStatus,
)
from complementary_services.domain.validation import InvalidServiceOffer, Issue, Violation
from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
AMOUNT_PATTERN = r"^\d{1,9}(\.\d{1,2})?$"


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


class AvailabilityInterval(BaseModel):
    """Intervalo `[starts_at, ends_at)`. En la solicitud, con desfase coherente con
    `time_zone`; en la respuesta, en UTC con sufijo `Z`."""

    model_config = ConfigDict(extra="forbid")

    starts_at: datetime
    ends_at: datetime
    time_zone: Annotated[str, Field(description="Zona IANA declarada, p. ej. America/Bogota.")]
    capacity: Annotated[int, Field(ge=CAPACITY_MIN, le=CAPACITY_MAX)]


class CreateServiceRequest(BaseModel):
    """Esquema documentado del cuerpo; la validación la hacen el transporte y el dominio."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[
        str,
        Field(
            min_length=NAME_MIN,
            max_length=NAME_MAX,
            description="Se recortan los espacios exteriores.",
        ),
    ]
    description: Annotated[
        str,
        Field(
            min_length=DESCRIPTION_MIN,
            max_length=DESCRIPTION_MAX,
            description="Se recortan los espacios exteriores.",
        ),
    ]
    category: Category
    modality: Modality
    price: ServicePrice
    availability: Annotated[
        list[AvailabilityInterval] | None,
        Field(
            max_length=100,
            description="Opcional; intervalos futuros de hasta 24 h, con fin dentro de 365 días, "
            "contiguos permitidos y sin superposición.",
        ),
    ] = None


class ServiceResponse(BaseModel):
    service_id: UUID
    provider_id: UUID
    name: str
    description: str
    category: Category
    modality: Modality
    price: ServicePrice
    availability: list[AvailabilityInterval]
    status: ServiceStatus
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(cls, view: ServiceView) -> "ServiceResponse":
        document = view.to_document()
        return cls.model_validate(document)


def _request_schema() -> dict[str, Any]:
    schema = CreateServiceRequest.model_json_schema(ref_template="#/components/schemas/{model}")
    schema.pop("$defs", None)
    return schema


def _invalid(violations: list[Violation] | tuple[Violation, ...]) -> Response:
    return error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        InvalidServiceOffer.code,
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


def build_services_router(
    *, verify_token: TokenVerifier, create_service: CreateService
) -> APIRouter:
    router = APIRouter(prefix="/v1/services", tags=["services"])

    @router.post(
        "",
        status_code=HTTPStatus.CREATED,
        response_model=ServiceResponse,
        summary="Crear un servicio en borrador (HU015)",
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
                "description": "`validation_error` con `details[{field, issue}]` (ADR-027).",
            },
        },
    )
    def create(
        response: Response,
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | ServiceResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        offer = parse_offer(body)
        key, key_problem = _idempotency_key(idempotency_key)
        try:
            if key is None:
                # La clave es inválida: se informan también las reglas de la oferta.
                problems = [key_problem] if key_problem else []
                try:
                    create_service.validate(caller, offer)
                except InvalidServiceOffer as error:
                    problems.extend(error.violations)
                return _invalid(problems)
            result = create_service(
                caller,
                CreateServiceCommand(
                    offer=offer, idempotency_key=key, correlation_id=correlation_uuid()
                ),
            )
        except PermissionRequired as error:
            return forbidden(error)
        except InvalidServiceOffer as error:
            return _invalid(error.violations)
        except IdempotencyKeyReused as error:
            return error_response(
                HTTPStatus.CONFLICT,
                error.code,
                "La clave de idempotencia ya se usó con otra solicitud.",
            )
        response.headers["Location"] = f"/v1/services/{result.service.offer.service_id}"
        if result.replayed:
            response.headers[REPLAYED_HEADER] = "true"
        return ServiceResponse.from_view(result.service)

    return router
