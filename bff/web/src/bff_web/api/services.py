"""HU015 en el canal web: `POST /v1/services` (contrato de ADR-027).

El BFF autentica, aplica el filtro de borde por permiso (`services.create`) y reenvía
el cuerpo y la clave de idempotencia sin reinterpretarlos: las reglas de la oferta y
los errores `422 validation_error` con `details[{field, issue}]` los decide el
servicio propietario y se entregan sin alterar. Los instantes llegan en UTC (`Z`); la
conversión a la zona del usuario corresponde al frontend.
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
from bff_web.clients.complementary_services import ComplementaryServicesClient
from bff_web.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.authorization import ActorPermissions
from sportapp_platform.errors import ErrorResponse
from sportapp_platform.tokens import VerifiedAccessToken

CREATE_PERMISSION = "services.create"
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"

Category = Literal["SPORTS_MEDICINE", "COACHING", "NUTRITION", "TRANSPORT", "ACCOMPANIMENT"]
Modality = Literal["IN_PERSON", "VIRTUAL", "HYBRID"]
BillingUnit = Literal["SERVICE", "SESSION", "HOUR", "DAY"]


class ServicePrice(BaseModel):
    """Precio público de referencia en COP, impuestos aplicables incluidos; no es un cobro."""

    model_config = ConfigDict(extra="forbid")

    amount: Annotated[
        str,
        Field(
            pattern=r"^\d{1,9}(\.\d{1,2})?$",
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
    capacity: Annotated[int, Field(ge=1, le=100)]


class CreateServiceRequest(BaseModel):
    """Esquema documentado del cuerpo; la validación la hace el servicio (ADR-027)."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[
        str, Field(min_length=3, max_length=120, description="Se recortan los espacios exteriores.")
    ]
    description: Annotated[
        str,
        Field(min_length=20, max_length=2000, description="Se recortan los espacios exteriores."),
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
    status: Literal["DRAFT", "PUBLISHED"]
    version: int
    created_at: datetime
    updated_at: datetime


def _request_schema() -> dict[str, Any]:
    schema = CreateServiceRequest.model_json_schema(ref_template="#/components/schemas/{model}")
    schema.pop("$defs", None)
    return schema


def build_services_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    permissions: ActorPermissions,
    services: ComplementaryServicesClient,
    channel: str,
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
                "description": "`permission_required` (`services.create`).",
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
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        actor = session.actor_type
        if actor is None or CREATE_PERMISSION not in permissions.for_actor(actor, channel):
            return _permission_required()
        try:
            upstream = services.create_service(session.token, body, idempotency_key)
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
