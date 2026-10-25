"""HU043 en el canal móvil: catálogo de alimentos y registro de consumos (ADR-037).

Opción base «Mi alimentación»: el BFF autentica (JWKS) y exige `athlete`, sin capacidad de
plan, antes de llamar a Entrenamientos; después reenvía cuerpo, `Idempotency-Key`,
`If-None-Match` y correlación sin reinterpretarlos. El deportista sale del token: el BFF no
acepta `athlete_id` (el servicio lo rechaza con `field_not_allowed`). El catálogo, sus unidades,
la idempotencia y los errores los decide `training_management`, que vuelve a verificar todo;
sus respuestas, `ETag` e `Idempotent-Replayed` llegan sin alterar. La app guarda el catálogo en
caché (`STALE` a las 24 h) y reintenta un consumo encolado solo ante red, `502`, `503` o `504`,
con el mismo `intake_id` y la misma clave.

Ningún alimento ni cantidad se escribe en los logs.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Body, Header, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from bff_mobile.clients.training_management import TrainingManagementClient
from bff_mobile.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.errors import ErrorResponse, error_response
from sportapp_platform.tokens import InvalidAccessToken, JwksUnavailable, VerifiedAccessToken

ATHLETE = "athlete"
BEARER_PREFIX = "bearer "
IDEMPOTENCY_HEADER = "Idempotency-Key"
IF_NONE_MATCH_HEADER = "If-None-Match"
REPLAYED_HEADER = "Idempotent-Replayed"
INSTANT = "Instante UTC con sufijo `Z` y precisión de segundos (`YYYY-MM-DDTHH:MM:SSZ`)."
QUANTITY = (
    "Cadena decimal con como máximo un decimal entre `0.1` y `5000.0`; el servicio la normaliza "
    "a un decimal (`150` → `150.0`)."
)
# Cabeceras del servicio que el cliente necesita tal como llegan.
RELAYED_HEADERS = ("ETag", "Cache-Control", REPLAYED_HEADER, "WWW-Authenticate")

_logger = logging.getLogger("sportapp.bff")

FoodCategory = Literal[
    "GRAIN", "PROTEIN", "DAIRY", "FRUIT", "VEGETABLE", "LEGUME", "FAT", "BEVERAGE", "SUPPLEMENT"
]
FoodUnit = Literal["G", "ML", "UNIT", "CUP", "TBSP"]
MealType = Literal["BREAKFAST", "LUNCH", "DINNER", "SNACK", "HYDRATION"]


class FoodCatalogItem(BaseModel):
    food_code: str
    label_key: Annotated[str, Field(description="Clave de traducción del nombre.")]
    category: FoodCategory
    default_unit: FoodUnit
    allowed_units: list[FoodUnit]
    allergens: list[str]
    incompatible_with: list[str]


class FoodCatalogResponse(BaseModel):
    """Catálogo cerrado y versionado; el `ETag` es su versión."""

    catalog_version: str
    items: list[FoodCatalogItem]


class FoodIntakeItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    food_code: Annotated[str, Field(description="Código de `GET /v1/food-catalog`.")]
    quantity: Annotated[str, Field(description=QUANTITY, examples=["150.0"])]
    unit: Annotated[FoodUnit, Field(description="Una de las `allowed_units` del alimento.")]


class RegisterFoodIntakeRequest(BaseModel):
    """Esquema documentado del consumo; la validación la hace el servicio (ADR-037)."""

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
    items: Annotated[list[FoodIntakeItemRequest], Field(min_length=1, max_length=20)]


class FoodIntakeItem(BaseModel):
    food_code: str
    quantity: Annotated[str, Field(description="Cadena con exactamente un decimal.")]
    unit: FoodUnit


class FoodIntakeResponse(BaseModel):
    """Consumo normalizado en la versión 1; sin calorías ni nutrientes."""

    intake_id: UUID
    athlete_id: UUID
    consumed_at: datetime
    meal_type: MealType
    items: list[FoodIntakeItem]
    catalog_version: str
    version: int
    registered_at: datetime


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    schema.pop("$defs", None)
    return {"requestBody": {"required": True, "content": {"application/json": {"schema": schema}}}}


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


def _unexpected(status: int) -> Response:
    _logger.warning(
        "bff.upstream_unexpected_status",
        extra={"fields": {"upstream": "training-management", "status": status}},
    )
    return _failure(UpstreamUnavailable())


def _passthrough(upstream: httpx.Response, allowed: set[int]) -> Response:
    if upstream.status_code not in allowed:
        return _unexpected(upstream.status_code)
    headers = {name: upstream.headers[name] for name in RELAYED_HEADERS if name in upstream.headers}
    if upstream.status_code == HTTPStatus.NOT_MODIFIED:
        return Response(status_code=HTTPStatus.NOT_MODIFIED, headers=headers)
    try:
        content = upstream.json()
    except ValueError:
        return _unexpected(upstream.status_code)
    return JSONResponse(status_code=upstream.status_code, content=content, headers=headers)


_COMMON: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNAUTHORIZED.value: {
        "model": ErrorResponse,
        "description": "`invalid_access_token`: falta el token o no es válido.",
    },
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required`: solo un deportista usa «Mi alimentación».",
    },
    HTTPStatus.SERVICE_UNAVAILABLE.value: {
        "model": ErrorResponse,
        "description": "`upstream_unavailable`: identidad no verificable o servicio caído.",
    },
    HTTPStatus.GATEWAY_TIMEOUT.value: {
        "model": ErrorResponse,
        "description": "`upstream_timeout`: el servicio no respondió a tiempo.",
    },
}
_ETAG = {"ETag": {"description": '`"<catalog_version>"`.', "schema": {"type": "string"}}}
_REPLAY = {
    REPLAYED_HEADER: {
        "description": "`true` cuando se devuelve la respuesta original de la misma clave.",
        "schema": {"type": "string"},
    }
}
_EDGE_STATUSES = {
    HTTPStatus.UNAUTHORIZED,
    HTTPStatus.FORBIDDEN,
    HTTPStatus.SERVICE_UNAVAILABLE,
    HTTPStatus.GATEWAY_TIMEOUT,
}


def build_food_intakes_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    training: TrainingManagementClient,
) -> APIRouter:
    router = APIRouter(tags=["nutrition"])

    def athlete_token(authorization: str | None) -> str | Response:
        """Borde: token válido de un deportista antes de llamar a Entrenamientos."""
        if not authorization or not authorization.lower().startswith(BEARER_PREFIX):
            return _unauthenticated()
        token = authorization[len(BEARER_PREFIX) :].strip()
        try:
            identity = verify_token(token)
            UUID(identity.subject)
        except (InvalidAccessToken, ValueError):
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
        "/v1/food-catalog",
        response_model=FoodCatalogResponse,
        summary="Catálogo de alimentos para registrar consumos (HU043, ADR-037)",
        responses={
            **_COMMON,
            HTTPStatus.OK.value: {"headers": _ETAG},
            HTTPStatus.NOT_MODIFIED.value: {
                "description": "`If-None-Match` coincide con el catálogo vigente; sin cuerpo.",
                "headers": _ETAG,
            },
        },
    )
    def food_catalog(
        if_none_match: Annotated[str | None, Header(alias=IF_NONE_MATCH_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = athlete_token(authorization)
        if isinstance(token, Response):
            return token
        return relay(
            lambda: training.food_catalog(token, if_none_match),
            {HTTPStatus.OK, HTTPStatus.NOT_MODIFIED, *_EDGE_STATUSES},
        )

    @router.post(
        "/v1/me/food-intakes",
        status_code=HTTPStatus.CREATED,
        response_model=FoodIntakeResponse,
        summary="Registrar un consumo de alimentos y bebidas (HU043, ADR-037)",
        openapi_extra=_schema(RegisterFoodIntakeRequest),
        responses={
            **_COMMON,
            HTTPStatus.CREATED.value: {
                "description": "Consumo registrado, respuesta original de la misma clave o "
                "consumo existente con el mismo `intake_id` y los mismos datos.",
                "headers": _REPLAY,
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`idempotency_key_reused` o `intake_id_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` con `details[{field, issue}]`: `required`, "
                "`field_not_allowed` (también `athlete_id`), `invalid_uuid`, "
                "`invalid_datetime`, `too_far_in_future`, `too_old`, `unknown_value` "
                "(`meal_type` o `food_code`), `unit_not_allowed`, `out_of_range`, `too_short` "
                "o `too_long` (`items` fuera de 1..20).",
            },
        },
    )
    def register_intake(
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = athlete_token(authorization)
        if isinstance(token, Response):
            return token
        return relay(
            lambda: training.register_food_intake(token, body, idempotency_key),
            {
                HTTPStatus.CREATED,
                HTTPStatus.CONFLICT,
                HTTPStatus.UNPROCESSABLE_ENTITY,
                *_EDGE_STATUSES,
            },
        )

    return router
