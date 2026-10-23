"""HU014 (ADR-037): planes de alimentación (contrato fijado el 2026-10-22).

- `POST /v1/nutrition-plans`: un `service_provider` con `nutrition.plan.author` y la
  autorización vigente del deportista crea y asigna el plan (`201` + `Location`); la misma
  clave devuelve el resultado original con `Idempotent-Replayed: true`.
- `GET /v1/nutrition-plans/{plan_id}`: el creador (con permiso y autorización vigente) o el
  deportista dueño; cualquier otro, `404` sin revelar si existe.
- `GET /v1/me/nutrition-plans`: planes del deportista autenticado con cursor opaco.

El profesional sale del token; el cuerpo solo identifica al deportista por `athlete_id`. Las
consultas leen la base de lectura y no exponen alergias ni restricciones del deportista.
"""

from datetime import date, datetime
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from training_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    correlation_uuid,
    forbidden,
)
from training_management.application.commands.create_nutrition_plan import (
    CreateNutritionPlan,
    CreateNutritionPlanCommand,
    ItemInput,
    MealInput,
)
from training_management.application.errors import (
    AthleteAuthorizationRequired,
    IdempotencyKeyReused,
    NutritionPlanNotFound,
    NutritionRestrictionsUnavailable,
    PermissionRequired,
)
from training_management.application.queries.nutrition_plans import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    GetNutritionPlan,
    InvalidNutritionPlanQuery,
    ListMyNutritionPlans,
    NutritionPlanFilters,
    NutritionPlanPage,
)
from training_management.domain.nutrition.catalog import FoodUnit
from training_management.domain.nutrition.events import nutrition_plan_document
from training_management.domain.nutrition.model import (
    ITEMS_PER_MEAL_MAX,
    MEALS_MAX,
    NUTRITION_PLAN_ISSUES,
    OBJECTIVE_MAX_LENGTH,
    OBJECTIVES_MAX,
    TITLE_MAX_LENGTH,
    InvalidNutritionPlan,
    MealType,
    NutritionPlan,
)

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
QUANTITY_DESCRIPTION = (
    "Cadena decimal con un decimal entre `0.1` y `5000.0`; se normaliza a un decimal "
    "(`150` → `150.0`). Fuera de rango o con más de un decimal: `nutrition_plan_invalid` "
    "con `out_of_range`."
)

Objective = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=OBJECTIVE_MAX_LENGTH)
]
Quantity = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9]{1,8}(\.[0-9]{1,8})?$", max_length=17),
    Field(description=QUANTITY_DESCRIPTION, examples=["150.0"]),
]
FoodCode = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64),
    Field(description="Código de `GET /v1/food-catalog`.", examples=["OATS"]),
]


class NutritionPlanItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    food_code: FoodCode
    quantity: Quantity
    unit: Annotated[FoodUnit, Field(description="Una de las `allowed_units` del alimento.")]


class NutritionMealRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meal_type: MealType
    items: Annotated[
        list[NutritionPlanItemRequest], Field(min_length=1, max_length=ITEMS_PER_MEAL_MAX)
    ]


class CreateNutritionPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    athlete_id: UUID
    title: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TITLE_MAX_LENGTH)
    ]
    objectives: Annotated[list[Objective], Field(min_length=1, max_length=OBJECTIVES_MAX)]
    start_date: date
    end_date: Annotated[
        date, Field(description="No anterior a `start_date`; periodo de hasta 26 semanas.")
    ]
    meals: Annotated[
        list[NutritionMealRequest],
        Field(
            min_length=1,
            max_length=MEALS_MAX,
            description="Menú diario del periodo, en el orden del profesional.",
        ),
    ]


class NutritionPlanItemResponse(BaseModel):
    food_code: str
    quantity: Annotated[str, Field(description="Cadena con exactamente un decimal.")]
    unit: FoodUnit


class NutritionMealResponse(BaseModel):
    meal_type: MealType
    items: list[NutritionPlanItemResponse]


class NutritionPlanResponse(BaseModel):
    plan_id: UUID
    athlete_id: UUID
    professional_id: UUID
    title: str
    objectives: list[str]
    start_date: date
    end_date: date
    status: Literal["ASSIGNED"]
    version: int
    catalog_version: str
    meals: list[NutritionMealResponse]
    created_at: datetime

    @classmethod
    def from_plan(cls, plan: NutritionPlan) -> "NutritionPlanResponse":
        return cls.model_validate(nutrition_plan_document(plan))


class CreatedNutritionPlanResponse(NutritionPlanResponse):
    warnings: list[Literal["nutrition_habits_missing"]] = Field(
        description="`nutrition_habits_missing` si el deportista no registró hábitos "
        "alimenticios: el plan se crea sin poder validar alergias ni restricciones."
    )


class NutritionPlansResponse(BaseModel):
    items: list[NutritionPlanResponse]
    next_cursor: str | None = Field(
        description="Cursor opaco de la página siguiente; `null` si no hay más."
    )

    @classmethod
    def from_page(cls, page: NutritionPlanPage) -> "NutritionPlansResponse":
        return cls(
            items=[NutritionPlanResponse.from_plan(plan) for plan in page.items],
            next_cursor=page.next_cursor,
        )


def _invalid_plan(error: InvalidNutritionPlan) -> Response:
    return error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        error.code,
        "El plan de alimentación no cumple las reglas o choca con las restricciones del "
        "deportista.",
        [ErrorDetail(field=f"body.{item.field}", issue=item.issue) for item in error.violations],
    )


def _not_found() -> Response:
    return error_response(
        HTTPStatus.NOT_FOUND, NutritionPlanNotFound.code, "El plan de alimentación no existe."
    )


def _users_unavailable() -> Response:
    return error_response(
        HTTPStatus.SERVICE_UNAVAILABLE,
        NutritionRestrictionsUnavailable.code,
        "No fue posible validar las restricciones alimenticias del deportista. Intente de nuevo.",
    )


def _limit(raw: str | None) -> int:
    """`limit` como entero; uno no entero se informa como `out_of_range`, igual que un valor
    fuera de 1..50, al validarlo el caso de uso."""
    if raw is None:
        return DEFAULT_LIMIT
    try:
        return int(raw.strip())
    except ValueError:
        return 0


def _command(body: CreateNutritionPlanRequest, key: UUID) -> CreateNutritionPlanCommand:
    return CreateNutritionPlanCommand(
        athlete_id=body.athlete_id,
        title=body.title,
        objectives=tuple(body.objectives),
        start_date=body.start_date,
        end_date=body.end_date,
        meals=tuple(
            MealInput(
                meal_type=meal.meal_type,
                items=tuple(
                    ItemInput(
                        food_code=item.food_code,
                        quantity=Decimal(item.quantity),
                        unit=item.unit.value,
                    )
                    for item in meal.items
                ),
            )
            for meal in body.meals
        ),
        idempotency_key=key,
        correlation_id=correlation_uuid(),
    )


_FORBIDDEN = {
    "model": ErrorResponse,
    "description": "`permission_required` (`nutrition.plan.author`) o "
    "`athlete_authorization_required`.",
}


def build_nutrition_plans_router(
    *,
    verify_token: TokenVerifier,
    create_plan: CreateNutritionPlan,
    get_plan: GetNutritionPlan,
    list_plans: ListMyNutritionPlans,
) -> APIRouter:
    router = APIRouter(tags=["nutrition-plans"])

    @router.post(
        "/v1/nutrition-plans",
        status_code=HTTPStatus.CREATED,
        response_model=CreatedNutritionPlanResponse,
        summary="Crear y asignar un plan de alimentación (HU014)",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.CREATED.value: {
                "headers": {
                    "Location": {
                        "description": "`/v1/nutrition-plans/{plan_id}`.",
                        "schema": {"type": "string"},
                    },
                    REPLAYED_HEADER: {
                        "description": "`true` cuando se devuelve el resultado de un reintento.",
                        "schema": {"type": "string"},
                    },
                }
            },
            HTTPStatus.FORBIDDEN.value: _FORBIDDEN,
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`idempotency_key_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` (forma del cuerpo) o `nutrition_plan_invalid` "
                "(reglas, catálogo, alergias y restricciones) con `details[{field, issue}]`. "
                "Motivos estables de `issue` en `nutrition_plan_invalid`: "
                + ", ".join(f"`{issue}`" for issue in NUTRITION_PLAN_ISSUES)
                + ". Los bloqueos llevan `field = body.meals.<i>.items.<j>.food_code`, uno por "
                "ítem y causa, y solo se evalúan cuando las reglas pasan.",
            },
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": "`upstream_unavailable`: no se pudo verificar la identidad o "
                "Usuarios no entregó las restricciones del deportista (el plan no se crea).",
            },
        },
    )
    def create(
        body: CreateNutritionPlanRequest,
        response: Response,
        idempotency_key: Annotated[UUID, Header(alias=IDEMPOTENCY_HEADER)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | CreatedNutritionPlanResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            result = create_plan(caller, _command(body, idempotency_key))
        except (PermissionRequired, AthleteAuthorizationRequired) as error:
            return forbidden(error)
        except InvalidNutritionPlan as error:
            return _invalid_plan(error)
        except IdempotencyKeyReused as error:
            return error_response(
                HTTPStatus.CONFLICT,
                error.code,
                "La clave de idempotencia ya se usó con otra solicitud.",
            )
        except NutritionRestrictionsUnavailable:
            return _users_unavailable()
        response.headers["Location"] = f"/v1/nutrition-plans/{result.plan.plan_id}"
        if result.replayed:
            response.headers[REPLAYED_HEADER] = "true"
        return CreatedNutritionPlanResponse.model_validate(
            {
                **nutrition_plan_document(result.plan),
                "warnings": list(result.warnings),
            }
        )

    @router.get(
        "/v1/nutrition-plans/{plan_id}",
        response_model=NutritionPlanResponse,
        summary="Consultar un plan de alimentación (HU014)",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` (otro tipo de actor o sin "
                "`nutrition.plan.author`) o `athlete_authorization_required` (el creador ya no "
                "tiene la autorización vigente del deportista).",
            },
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": "`nutrition_plan_not_found`: no existe, no es visible para "
                "quien consulta o aún no está proyectado.",
            },
        },
    )
    def get(
        plan_id: UUID,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | NutritionPlanResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            plan = get_plan(caller, plan_id)
        except NutritionPlanNotFound:
            return _not_found()
        except (PermissionRequired, AthleteAuthorizationRequired) as error:
            return forbidden(error)
        response.headers["Cache-Control"] = "no-store"
        return NutritionPlanResponse.from_plan(plan)

    @router.get(
        "/v1/me/nutrition-plans",
        response_model=NutritionPlansResponse,
        summary="Mis planes de alimentación (HU014, lectura CQRS)",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required`: solo para deportistas.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `query.limit` (`out_of_range`, también si "
                "no es un entero) o `query.cursor` (`invalid_cursor`).",
            },
        },
    )
    def my_plans(
        *,
        response: Response,
        limit: Annotated[
            str | None,
            Query(description=f"Entero de 1 a {MAX_LIMIT}; {DEFAULT_LIMIT} por defecto."),
        ] = None,
        cursor: Annotated[str | None, Query(description="`next_cursor` anterior.")] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | NutritionPlansResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            page = list_plans(caller, NutritionPlanFilters(limit=_limit(limit), cursor=cursor))
        except PermissionRequired as error:
            return forbidden(error)
        except InvalidNutritionPlanQuery as error:
            return error_response(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                error.code,
                "La consulta no es válida.",
                [ErrorDetail(field=f"query.{error.field}", issue=error.issue)],
            )
        response.headers["Cache-Control"] = "no-store"
        return NutritionPlansResponse.from_page(page)

    return router
