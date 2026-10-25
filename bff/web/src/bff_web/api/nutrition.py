"""HU014 y HU013 en el canal web: planes de alimentación, catálogo de alimentos e historial
de consumos (ADR-037).

El BFF autentica (JWKS), aplica el filtro de borde por actor y permiso, resuelve el correo
del deportista con el contrato interno de Usuarios (como HU010) y reenvía al servicio
propietario con el token del usuario. Las reglas del plan (fechas, catálogo, unidades,
cantidades, alergias y restricciones), la autorización del deportista y la idempotencia
viven en `training_management`, que vuelve a verificarlo todo: el BFF no las duplica y
entrega sus respuestas sin alterar.

Para no revelar si un correo pertenece a un deportista, la creación responde
`403 athlete_authorization_required` tanto si el correo no existe o es de otro actor como
si el deportista no autorizó al profesional. El profesional nunca lee los hábitos del
deportista: el BFF no expone la consulta de restricciones de Usuarios.

HU013: `GET /v1/me/food-intakes` (deportista) y `GET /v1/athletes/{athlete_id}/food-intakes`
(profesional con `nutrition.history.view_assigned`) reenvían los filtros al servicio, que
vuelve a verificar el permiso y la autorización vigente, audita el acceso del profesional y
valida periodo y cursor. Los textos de los alimentos los resuelve la web con
`GET /v1/food-catalog`; el BFF no calcula nutrientes.
"""

from collections.abc import Callable
from datetime import date, datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

from bff_web.api.training import (
    _UNAUTHORIZED,
    _UPSTREAM,
    ATHLETE,
    ATHLETE_AUTHORIZATION_REQUIRED,
    IDEMPOTENCY_HEADER,
    REPLAYED_HEADER,
    _passthrough,
    _permission_required,
    _Session,
    _upstream_failure,
    authenticate_session,
)
from bff_web.clients.training_management import IF_NONE_MATCH_HEADER, TrainingManagementClient
from bff_web.clients.users_management import (
    UpstreamTimeout,
    UpstreamUnavailable,
    UsersManagementClient,
)
from sportapp_platform.authorization import ActorPermissions
from sportapp_platform.errors import ErrorResponse, error_response
from sportapp_platform.tokens import VerifiedAccessToken

NUTRITION_AUTHOR_PERMISSION = "nutrition.plan.author"
NUTRITION_HISTORY_PERMISSION = "nutrition.history.view_assigned"
INTAKES_DEFAULT_LIMIT = 20
INTAKES_MAX_LIMIT = 100
ETAG_HEADER = "ETag"
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
# Motivos estables de `details[].issue` en `nutrition_plan_invalid` (ADR-037). El BFF no los
# valida: reenvía el `422` del servicio; la lista solo documenta el contrato para la web.
NUTRITION_PLAN_RULE_ISSUES = (
    "end_date_before_start_date",
    "period_too_long",
    "unknown_value",
    "unit_not_allowed",
    "out_of_range",
    "conflicts_with_allergy",
    "conflicts_with_restriction",
)

QUANTITY_DESCRIPTION = (
    "Cadena decimal con un decimal entre `0.1` y `5000.0`; se normaliza a un decimal "
    "(`150` → `150.0`). Fuera de rango o con más de un decimal: `nutrition_plan_invalid` "
    "con `out_of_range`."
)

# HU013: esquema del consumo idéntico al de `training-management` (HU043), que el BFF reenvía.
INTAKE_QUANTITY = (
    "Cadena decimal con como máximo un decimal entre `0.1` y `5000.0`; se normaliza a un "
    "decimal (`150` → `150.0`). Un número JSON o un valor fuera de rango es `out_of_range`."
)


# Enumeraciones con el mismo nombre y valores que el OpenAPI del servicio (prueba de contrato).
class MealType(StrEnum):
    BREAKFAST = "BREAKFAST"
    LUNCH = "LUNCH"
    DINNER = "DINNER"
    SNACK = "SNACK"
    HYDRATION = "HYDRATION"


class FoodUnit(StrEnum):
    G = "G"
    ML = "ML"
    UNIT = "UNIT"
    CUP = "CUP"
    TBSP = "TBSP"


class FoodCategory(StrEnum):
    GRAIN = "GRAIN"
    PROTEIN = "PROTEIN"
    DAIRY = "DAIRY"
    FRUIT = "FRUIT"
    VEGETABLE = "VEGETABLE"
    LEGUME = "LEGUME"
    FAT = "FAT"
    BEVERAGE = "BEVERAGE"
    SUPPLEMENT = "SUPPLEMENT"


Objective = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
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


class NutritionPlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    food_code: FoodCode
    quantity: Quantity
    unit: Annotated[FoodUnit, Field(description="Una de las `allowed_units` del alimento.")]


class NutritionPlanMeal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meal_type: MealType
    items: Annotated[list[NutritionPlanItem], Field(min_length=1, max_length=15)]


class CreateNutritionPlanRequest(BaseModel):
    """Cuerpo del servicio con `athlete_email` en lugar de `athlete_id` (prueba de contrato).

    Forma y longitudes iguales a las del servicio; las reglas del plan (fechas, catálogo,
    unidades, cantidades, alergias y restricciones) solo las evalúa el servicio.
    """

    model_config = ConfigDict(extra="forbid")

    athlete_email: Annotated[EmailStr, Field(max_length=254)]
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    objectives: Annotated[list[Objective], Field(min_length=1, max_length=5)]
    start_date: date
    end_date: Annotated[
        date, Field(description="No anterior a `start_date`; periodo de hasta 26 semanas.")
    ]
    meals: Annotated[
        list[NutritionPlanMeal],
        Field(
            min_length=1,
            max_length=8,
            description="Menú diario del periodo, en el orden del profesional.",
        ),
    ]


class NutritionPlanItemResponse(BaseModel):
    food_code: str
    quantity: Annotated[str, Field(description="Cadena con exactamente un decimal.")]
    unit: FoodUnit


class NutritionPlanMealResponse(BaseModel):
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
    meals: list[NutritionPlanMealResponse]
    created_at: datetime


class CreatedNutritionPlanResponse(NutritionPlanResponse):
    warnings: list[Literal["nutrition_habits_missing"]] = Field(
        description="`nutrition_habits_missing` si el deportista no registró hábitos "
        "alimenticios: el plan se crea sin poder validar alergias ni restricciones."
    )


class NutritionPlansPage(BaseModel):
    items: list[NutritionPlanResponse]
    next_cursor: str | None = Field(
        description="Cursor opaco de la página siguiente; `null` si no hay más."
    )


class FoodCatalogItem(BaseModel):
    food_code: str
    label_key: str
    category: FoodCategory
    default_unit: FoodUnit
    allowed_units: list[FoodUnit]
    allergens: list[str]
    incompatible_with: list[str]


class FoodCatalogResponse(BaseModel):
    catalog_version: str
    items: list[FoodCatalogItem]


class FoodIntakeItem(BaseModel):
    """Alimento del consumo; el mismo esquema en la solicitud y en la respuesta (normalizada
    con exactamente un decimal)."""

    model_config = ConfigDict(extra="forbid")

    food_code: Annotated[str, Field(description="Código de `GET /v1/food-catalog`.")]
    quantity: Annotated[str, Field(description=INTAKE_QUANTITY, examples=["150.0"])]
    unit: Annotated[FoodUnit, Field(description="Una de las `allowed_units` del alimento.")]


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


class FoodIntakesPage(BaseModel):
    items: list[FoodIntakeResponse] = Field(
        description="Consumos de `consumed_at` y `intake_id` descendentes; vacía si no hay."
    )
    next_cursor: str | None = Field(
        description="Cursor opaco de la página siguiente; `null` si no hay más."
    )


_ETAG = {ETAG_HEADER: {"description": "Versión del catálogo.", "schema": {"type": "string"}}}


def _error(status: HTTPStatus, description: str) -> dict[int | str, dict[str, Any]]:
    return {status.value: {"model": ErrorResponse, "description": description}}


def _catalog_relay(upstream: httpx.Response) -> Response:
    """`200` con cuerpo o `304` sin cuerpo; ambos con el `ETag` del servicio."""
    if upstream.status_code == HTTPStatus.NOT_MODIFIED:
        etag = upstream.headers.get(ETAG_HEADER)
        headers = {ETAG_HEADER: etag} if etag is not None else {}
        return Response(status_code=HTTPStatus.NOT_MODIFIED, headers=headers)
    return _passthrough(upstream, {HTTPStatus.OK}, headers=(ETAG_HEADER,))


_INTAKE_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_UNAUTHORIZED,
    **_UPSTREAM,
    **_error(
        HTTPStatus.FORBIDDEN,
        f"`permission_required` o `{ATHLETE_AUTHORIZATION_REQUIRED}` (HU013 CA3), sin revelar si "
        "el deportista existe.",
    ),
    **_error(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "`validation_error`: periodo (`query.to` `to_before_from`), límite, tipo de comida o "
        "cursor (`query.cursor` `invalid_cursor`) inválidos (HU013 CA2).",
    ),
}
DATE_FROM = "Primer día del periodo (inclusivo, fecha de consumo en UTC)."
DATE_TO = "Último día del periodo (inclusivo, fecha de consumo en UTC); no anterior a `from`."


def _intake_query(
    date_from: date | None,
    date_to: date | None,
    meal_type: MealType | None,
    limit: int,
    cursor: str | None,
) -> dict[str, str]:
    pairs = (
        ("from", date_from.isoformat() if date_from else None),
        ("to", date_to.isoformat() if date_to else None),
        ("meal_type", meal_type.value if meal_type else None),
        ("limit", str(limit)),
        ("cursor", cursor),
    )
    return {name: value for name, value in pairs if value is not None}


def build_nutrition_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    permissions: ActorPermissions,
    users: UsersManagementClient,
    training: TrainingManagementClient,
    channel: str,
) -> APIRouter:
    router = APIRouter(tags=["nutrition"])

    @router.post(
        "/v1/nutrition-plans",
        status_code=HTTPStatus.CREATED,
        response_model=CreatedNutritionPlanResponse,
        summary="Crear y asignar un plan de alimentación (HU014)",
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
            **_error(
                HTTPStatus.FORBIDDEN,
                f"`permission_required` (`{NUTRITION_AUTHOR_PERMISSION}`) o "
                f"`{ATHLETE_AUTHORIZATION_REQUIRED}` (correo inexistente, de otro actor o sin "
                "autorización vigente).",
            ),
            **_error(HTTPStatus.CONFLICT, "`idempotency_key_reused`."),
            **_error(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "`validation_error` (forma del cuerpo) o `nutrition_plan_invalid` con "
                "`details[{field, issue}]`. Motivos estables de `issue`: "
                + ", ".join(f"`{issue}`" for issue in NUTRITION_PLAN_RULE_ISSUES)
                + ".",
            ),
        },
    )
    def create_plan(
        body: CreateNutritionPlanRequest,
        idempotency_key: Annotated[UUID, Header(alias=IDEMPOTENCY_HEADER)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        actor = session.actor_type
        if actor is None or NUTRITION_AUTHOR_PERMISSION not in permissions.for_actor(
            actor, channel
        ):
            return _permission_required()
        try:
            athlete = users.lookup_user(str(body.athlete_email))
            if athlete is None or athlete.get("actor_type") != ATHLETE:
                return error_response(
                    HTTPStatus.FORBIDDEN,
                    ATHLETE_AUTHORIZATION_REQUIRED,
                    "El deportista no ha autorizado a este profesional para asignarle planes.",
                )
            payload = body.model_dump(mode="json", exclude={"athlete_email"}) | {
                "athlete_id": str(athlete["user_id"])
            }
            upstream = training.create_nutrition_plan(session.token, payload, idempotency_key)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        return _passthrough(
            upstream,
            {
                HTTPStatus.CREATED,
                HTTPStatus.FORBIDDEN,
                HTTPStatus.CONFLICT,
                HTTPStatus.UNPROCESSABLE_ENTITY,
                HTTPStatus.SERVICE_UNAVAILABLE,
            },
            headers=("Location", REPLAYED_HEADER),
        )

    @router.get(
        "/v1/nutrition-plans/{plan_id}",
        response_model=NutritionPlanResponse,
        summary="Consultar un plan de alimentación (HU014)",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            **_error(
                HTTPStatus.FORBIDDEN,
                "`permission_required` (otro actor o sin permiso) o "
                f"`{ATHLETE_AUTHORIZATION_REQUIRED}` (creador con la autorización revocada).",
            ),
            **_error(
                HTTPStatus.NOT_FOUND,
                "`nutrition_plan_not_found`: no existe, no es visible o aún no está proyectado.",
            ),
        },
    )
    def get_plan(
        plan_id: UUID,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        # Borde (CA3): solo el deportista o un autor de planes; el servicio decide la propiedad.
        actor = session.actor_type
        if actor != ATHLETE and (
            actor is None
            or NUTRITION_AUTHOR_PERMISSION not in permissions.for_actor(actor, channel)
        ):
            return _permission_required()
        try:
            upstream = training.get_nutrition_plan(session.token, plan_id)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        response = _passthrough(
            upstream, {HTTPStatus.OK, HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND}
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get(
        "/v1/me/nutrition-plans",
        response_model=NutritionPlansPage,
        summary="Mis planes de alimentación (HU014, lectura CQRS)",
        description="Solo `athlete`. Orden `created_at`, `plan_id` descendente; el cursor es "
        "opaco.",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            **_error(HTTPStatus.FORBIDDEN, "`permission_required`: solo el deportista."),
            **_error(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "`validation_error` del servicio en `query.limit` (`out_of_range`, también si "
                "no es entero) o `query.cursor` (`invalid_cursor`), sin alterar.",
            ),
        },
    )
    def my_plans(
        *,
        limit: Annotated[
            str | None,
            Query(description=f"Entero de 1 a {MAX_LIMIT}; {DEFAULT_LIMIT} por defecto."),
        ] = None,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        if session.actor_type != ATHLETE:
            return _permission_required()
        # El servicio valida `limit` y `cursor` con sus motivos estables: se reenvían tal cual.
        params = {name: value for name, value in (("limit", limit), ("cursor", cursor)) if value}
        try:
            upstream = training.list_my_nutrition_plans(session.token, params)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        response = _passthrough(
            upstream, {HTTPStatus.OK, HTTPStatus.FORBIDDEN, HTTPStatus.UNPROCESSABLE_ENTITY}
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get(
        "/v1/food-catalog",
        response_model=FoodCatalogResponse,
        summary="Catálogo cerrado de alimentos (HU014)",
        description="Cualquier actor autenticado. Con `If-None-Match` igual al `ETag` vigente "
        "responde `304` sin cuerpo.",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
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
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        try:
            upstream = training.food_catalog(session.token, if_none_match)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        return _catalog_relay(upstream)

    _add_food_intake_routes(
        router,
        verify_token=verify_token,
        permissions=permissions,
        training=training,
        channel=channel,
    )

    return router


def _add_food_intake_routes(
    router: APIRouter,
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    permissions: ActorPermissions,
    training: TrainingManagementClient,
    channel: str,
) -> None:
    """HU013: historial de consumos propio y del deportista que autorizó al profesional."""

    def forward_intakes(
        session: _Session, athlete_id: UUID | None, params: dict[str, str]
    ) -> Response:
        try:
            upstream = training.list_food_intakes(session.token, athlete_id, params)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        # El servicio decide la autorización final y valida periodo y cursor: sin alterar.
        response = _passthrough(
            upstream, {HTTPStatus.OK, HTTPStatus.FORBIDDEN, HTTPStatus.UNPROCESSABLE_ENTITY}
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get(
        "/v1/me/food-intakes",
        response_model=FoodIntakesPage,
        summary="Mi historial de alimentación (HU013)",
        description="Solo `athlete` (opción base «Mi alimentación»). Los planes asignados se "
        "consultan con `GET /v1/me/nutrition-plans` y los textos de los alimentos con "
        "`GET /v1/food-catalog`.",
        responses=_INTAKE_RESPONSES,
    )
    def my_food_intakes(
        *,
        date_from: Annotated[date | None, Query(alias="from", description=DATE_FROM)] = None,
        date_to: Annotated[date | None, Query(alias="to", description=DATE_TO)] = None,
        meal_type: MealType | None = None,
        limit: Annotated[int, Query(ge=1, le=INTAKES_MAX_LIMIT)] = INTAKES_DEFAULT_LIMIT,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        if session.actor_type != ATHLETE:
            return _permission_required()
        return forward_intakes(
            session, None, _intake_query(date_from, date_to, meal_type, limit, cursor)
        )

    @router.get(
        "/v1/athletes/{athlete_id}/food-intakes",
        response_model=FoodIntakesPage,
        summary="Historial de alimentación de un deportista que me autorizó (HU013)",
        description=f"Solo `service_provider` con `{NUTRITION_HISTORY_PERMISSION}` y autorización "
        "vigente del deportista (HU010); el servicio audita cada acceso. Solo consumos: nunca "
        "hábitos ni perfil del deportista.",
        responses=_INTAKE_RESPONSES,
    )
    def athlete_food_intakes(
        *,
        athlete_id: UUID,
        date_from: Annotated[date | None, Query(alias="from", description=DATE_FROM)] = None,
        date_to: Annotated[date | None, Query(alias="to", description=DATE_TO)] = None,
        meal_type: MealType | None = None,
        limit: Annotated[int, Query(ge=1, le=INTAKES_MAX_LIMIT)] = INTAKES_DEFAULT_LIMIT,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        actor = session.actor_type
        if actor is None or NUTRITION_HISTORY_PERMISSION not in permissions.for_actor(
            actor, channel
        ):
            return _permission_required()
        return forward_intakes(
            session, athlete_id, _intake_query(date_from, date_to, meal_type, limit, cursor)
        )
