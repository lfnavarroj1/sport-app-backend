"""HU043 (ADR-037): `POST /v1/me/food-intakes`.

El deportista registra lo que comió o bebió con alimentos de `GET /v1/food-catalog`
(HU014). `201` con el consumo normalizado en la versión 1; la misma clave devuelve la
respuesta original (`201` + `Idempotent-Replayed: true`) y el mismo `intake_id` con los
mismos datos y otra clave devuelve el consumo existente (`201`, sin otro evento).

Solo `athlete`; el deportista sale del token (`athlete_id` en el cuerpo es
`field_not_allowed`). El cuerpo se lee como JSON sin validación automática para responder
`validation_error` con `details[{field, issue}]`; su esquema se documenta en el OpenAPI.
"""

from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Header, Response
from pydantic import BaseModel, ConfigDict, Field

from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from training_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    correlation_uuid,
    forbidden,
)
from training_management.application.commands.register_food_intake import (
    IDEMPOTENCY_HEADER,
    IntakeOutcome,
    RegisterFoodIntake,
    RegisterFoodIntakeCommand,
)
from training_management.application.errors import (
    IdempotencyKeyReused,
    IntakeIdReused,
    PermissionRequired,
)
from training_management.domain.nutrition.catalog import FoodUnit
from training_management.domain.nutrition.intake import (
    ITEMS_MAX,
    ITEMS_MIN,
    FoodIntake,
    InvalidFoodIntake,
)
from training_management.domain.nutrition.intake_events import food_intake_document
from training_management.domain.nutrition.model import MealType

REPLAYED_HEADER = "Idempotent-Replayed"
INSTANT = "Instante UTC con sufijo `Z` y precisión de segundos (`YYYY-MM-DDTHH:MM:SSZ`)."
QUANTITY_DESCRIPTION = (
    "Cadena decimal con como máximo un decimal entre `0.1` y `5000.0`; se normaliza a un "
    "decimal (`150` → `150.0`). Un número JSON o un valor fuera de rango es `out_of_range`."
)
ISSUES = (
    "`required`, `field_not_allowed` (también `athlete_id`), `invalid_uuid` (`intake_id` no "
    "UUID v4 o `Idempotency-Key` no UUID), `invalid_datetime`, `too_far_in_future` (más de 5 "
    "minutos), `too_old` (más de 30 días), `unknown_value` (`meal_type` o `food_code` fuera "
    "del catálogo), `unit_not_allowed`, `out_of_range` (cantidad), `too_short` y `too_long` "
    f"(`items` fuera de {ITEMS_MIN}..{ITEMS_MAX})"
)

_CONFLICTS: dict[type[Exception], str] = {
    IdempotencyKeyReused: "La clave de idempotencia ya se usó con otra solicitud.",
    IntakeIdReused: "El identificador del consumo ya se usó con otros datos.",
}


class FoodIntakeItem(BaseModel):
    """Alimento del consumo; el mismo esquema en la solicitud y en la respuesta (normalizada
    con exactamente un decimal)."""

    model_config = ConfigDict(extra="forbid")

    food_code: Annotated[str, Field(description="Código de `GET /v1/food-catalog`.")]
    quantity: Annotated[str, Field(description=QUANTITY_DESCRIPTION, examples=["150.0"])]
    unit: Annotated[FoodUnit, Field(description="Una de las `allowed_units` del alimento.")]


class RegisterFoodIntakeRequest(BaseModel):
    """Esquema documentado del consumo; la validación la hace el dominio (ADR-037)."""

    model_config = ConfigDict(extra="forbid")

    intake_id: Annotated[UUID, Field(description="UUID v4 generado por el móvil.")]
    consumed_at: Annotated[
        datetime,
        Field(
            description=f"{INSTANT} Hasta 5 minutos en el futuro y 30 días en el pasado.",
            examples=["2026-10-24T12:30:00Z"],
        ),
    ]
    meal_type: MealType
    items: Annotated[
        list[FoodIntakeItem],
        Field(min_length=ITEMS_MIN, max_length=ITEMS_MAX, description="En el orden enviado."),
    ]


class FoodIntakeResponse(BaseModel):
    """Consumo normalizado; sin calorías ni nutrientes."""

    intake_id: UUID
    athlete_id: Annotated[UUID, Field(description="Deportista del token.")]
    consumed_at: datetime
    meal_type: MealType
    items: list[FoodIntakeItem]
    catalog_version: str
    version: int
    registered_at: datetime

    @classmethod
    def from_intake(cls, intake: FoodIntake) -> "FoodIntakeResponse":
        return cls.model_validate(food_intake_document(intake))


def _request_body() -> dict[str, Any]:
    schema = RegisterFoodIntakeRequest.model_json_schema(
        ref_template="#/components/schemas/{model}"
    )
    schema.pop("$defs", None)
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": schema}},
        }
    }


def _failure(error: Exception) -> Response:
    if isinstance(error, PermissionRequired):
        return forbidden(error)
    if isinstance(error, InvalidFoodIntake):
        return error_response(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            error.code,
            "La solicitud no es válida.",
            [ErrorDetail(field=item.field, issue=item.issue.value) for item in error.violations],
        )
    return error_response(HTTPStatus.CONFLICT, getattr(error, "code", ""), _CONFLICTS[type(error)])


def build_food_intakes_router(
    *, verify_token: TokenVerifier, register: RegisterFoodIntake
) -> APIRouter:
    router = APIRouter(tags=["food-intakes"])

    @router.post(
        "/v1/me/food-intakes",
        status_code=HTTPStatus.CREATED,
        response_model=FoodIntakeResponse,
        summary="Registrar un consumo de alimentos y bebidas (HU043, ADR-037)",
        openapi_extra=_request_body(),
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.CREATED.value: {
                "description": "Consumo registrado, respuesta original de la misma clave o "
                "consumo existente con el mismo `intake_id` y los mismos datos.",
                "headers": {
                    REPLAYED_HEADER: {
                        "description": "`true` cuando se devuelve la respuesta original de la "
                        "misma clave.",
                        "schema": {"type": "string"},
                    }
                },
            },
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required`: solo un deportista registra consumos.",
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`idempotency_key_reused` o `intake_id_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": f"`validation_error` con `details[{{field, issue}}]`: {ISSUES}.",
            },
        },
    )
    def register_intake(
        response: Response,
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | FoodIntakeResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            result = register(
                caller,
                RegisterFoodIntakeCommand(
                    body=body, idempotency_key=idempotency_key, correlation_id=correlation_uuid()
                ),
            )
        except (
            PermissionRequired,
            InvalidFoodIntake,
            IdempotencyKeyReused,
            IntakeIdReused,
        ) as error:
            return _failure(error)
        if result.outcome is IntakeOutcome.REPLAYED:
            response.headers[REPLAYED_HEADER] = "true"
        return FoodIntakeResponse.from_intake(result.intake)

    return router
