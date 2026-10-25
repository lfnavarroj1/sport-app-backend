"""HU030 (ADR-041): rutinas alimenticias sugeribles.

- `POST /v1/nutrition-routine-templates`: el `service_provider` con `nutrition.plan.author`
  publica una rutina (`201` + `Location`); la misma clave devuelve el resultado original con
  `Idempotent-Replayed: true`.
- `POST /v1/nutrition-routine-templates/{template_id}/retire`: retira una rutina propia con
  `expected_version`; una ajena o inexistente es `404 template_not_found`.
- `GET /v1/me/nutrition-routine-templates`: rutinas del profesional autenticado (lectura CQRS).
- `GET /v1/me/suggestions/nutrition-routines`: sugerencias para el `athlete` con
  `nutrition.routines.suggest`; `bff-mobile` envía patrón, alergias, deportes y objetivo como
  filtros y la exclusión ocurre aquí.

El profesional sale del token. Una rutina no es un plan asignado (HU014) y no incluye envío,
compra ni pago (CA5). Las alergias recibidas solo filtran: no se persisten ni se registran.
"""

from datetime import datetime
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
from training_management.application.access import (
    NUTRITION_PLAN_AUTHOR_PERMISSION,
    ROUTINE_SUGGEST_CAPABILITY,
)
from training_management.application.commands.nutrition_routine_templates import (
    PublishRoutine,
    PublishRoutineCommand,
    RetireRoutine,
    RetireRoutineCommand,
)
from training_management.application.errors import (
    CapabilitiesTimeout,
    CapabilitiesUnavailable,
    CapabilityRequired,
    IdempotencyKeyReused,
    PermissionRequired,
)
from training_management.application.queries.nutrition_routine_templates import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    InvalidRoutineQuery,
    ListMyRoutines,
    MyRoutinesFilters,
    RoutinePage,
    RoutineSuggestionFilters,
    RoutineSuggestions,
    SuggestRoutines,
)
from training_management.domain.nutrition.model import MealType
from training_management.domain.plans.model import Sport
from training_management.domain.routines.events import routine_document
from training_management.domain.routines.model import (
    ALLERGENS_MAX,
    DESCRIPTION_MAX,
    DESCRIPTION_MIN,
    DURATION_DAYS_MAX,
    DURATION_DAYS_MIN,
    ITEMS_MAX,
    ITEMS_MIN,
    MEALS_MAX,
    MEALS_MIN,
    PORTIONS_MAX,
    PORTIONS_MIN,
    TITLE_MAX,
    TITLE_MIN,
    Allergy,
    DietPattern,
    InvalidRoutine,
    NutritionRoutineTemplate,
    RoutineInput,
    RoutineItem,
    RoutineMeal,
    RoutineNotFound,
    RoutineVersionConflict,
)
from training_management.domain.templates.model import Goal

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
BASE = "/v1/nutrition-routine-templates"
FOOD_CODE_MAX = 64


class RoutineItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    food_code: Annotated[
        str,
        StringConstraints(min_length=1, max_length=FOOD_CODE_MAX),
        Field(description="Código del catálogo de alimentos (`GET /v1/food-catalog`)."),
    ]
    portions: Annotated[int, Field(ge=PORTIONS_MIN, le=PORTIONS_MAX, strict=True)]


class RoutineMealRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meal: MealType
    items: Annotated[list[RoutineItemRequest], Field(min_length=ITEMS_MIN, max_length=ITEMS_MAX)]


class PublishRoutineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=TITLE_MIN, max_length=TITLE_MAX)
    ]
    description: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=DESCRIPTION_MIN, max_length=DESCRIPTION_MAX
        ),
    ]
    goal: Annotated[Goal, Field(description="Catálogo de objetivos de ADR-034.")]
    sport: Annotated[Sport | None, Field(description="Deporte opcional (catálogo de HU010).")] = (
        None
    )
    dietary_pattern: Annotated[
        DietPattern, Field(description="Patrón alimenticio de la rutina (catálogo de HU009).")
    ]
    contains_allergens: Annotated[
        list[Allergy],
        Field(
            max_length=ALLERGENS_MAX,
            description="Alérgenos que contiene (catálogo de HU009), sin repetir; puede ir "
            "vacía. El servicio agrega los alérgenos del catálogo de cada alimento.",
        ),
    ]
    duration_days: Annotated[int, Field(ge=DURATION_DAYS_MIN, le=DURATION_DAYS_MAX, strict=True)]
    daily_meals: Annotated[
        list[RoutineMealRequest],
        Field(
            min_length=MEALS_MIN,
            max_length=MEALS_MAX,
            description="Comidas de un día tipo, en el orden del profesional.",
        ),
    ]


class RetireRoutineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[
        int, Field(ge=1, strict=True, description="Versión vigente que conoce el cliente.")
    ]


class RoutineItemResponse(BaseModel):
    food_code: str
    portions: int


class RoutineMealResponse(BaseModel):
    meal: MealType
    items: list[RoutineItemResponse]


class RoutineResponse(BaseModel):
    template_id: UUID
    professional_id: UUID
    title: str
    description: str
    goal: Goal
    sport: Sport | None
    dietary_pattern: DietPattern
    contains_allergens: list[Allergy] = Field(description="En el orden del catálogo de HU009.")
    duration_days: int = Field(description="Vigencia de la rutina en días.")
    daily_meals: list[RoutineMealResponse]
    status: Literal["PUBLISHED", "RETIRED"]
    version: int
    created_at: datetime
    updated_at: datetime
    published_at: datetime
    retired_at: datetime | None

    @classmethod
    def from_routine(cls, routine: NutritionRoutineTemplate) -> "RoutineResponse":
        return cls.model_validate(routine_document(routine))


class RoutinesResponse(BaseModel):
    items: list[RoutineResponse]
    next_cursor: str | None = Field(
        description="Cursor opaco de la página siguiente; `null` si no hay más."
    )

    @classmethod
    def from_page(cls, page: RoutinePage) -> "RoutinesResponse":
        return cls(
            items=[RoutineResponse.from_routine(item) for item in page.items],
            next_cursor=page.next_cursor,
        )


class RoutineSuggestionResponse(RoutineResponse):
    reasons: list[Literal["RESTRICTION_COMPATIBLE", "GOAL_MATCH", "SPORT_MATCH"]] = Field(
        description="Motivos de la sugerencia; vacío en la lista no personalizada."
    )


class RoutineSuggestionsResponse(BaseModel):
    items: list[RoutineSuggestionResponse]
    personalized: bool = Field(
        description="`false` si no llegó `dietary_pattern` (sin hábitos alimenticios): últimas "
        "publicadas, sin motivos y sin verificar restricciones."
    )

    @classmethod
    def from_result(cls, result: RoutineSuggestions) -> "RoutineSuggestionsResponse":
        return cls(
            items=[
                RoutineSuggestionResponse.model_validate(
                    {
                        **routine_document(item.routine),
                        "reasons": [reason.value for reason in item.reasons],
                    }
                )
                for item in result.items
            ],
            personalized=result.personalized,
        )


def _publish_command(body: PublishRoutineRequest, key: UUID) -> PublishRoutineCommand:
    return PublishRoutineCommand(
        data=RoutineInput(
            title=body.title,
            description=body.description,
            goal=body.goal,
            sport=body.sport,
            dietary_pattern=body.dietary_pattern,
            contains_allergens=tuple(body.contains_allergens),
            duration_days=body.duration_days,
            daily_meals=tuple(
                RoutineMeal(
                    meal=meal.meal,
                    items=tuple(
                        RoutineItem(food_code=item.food_code, portions=item.portions)
                        for item in meal.items
                    ),
                )
                for meal in body.daily_meals
            ),
        ),
        idempotency_key=key,
        correlation_id=correlation_uuid(),
    )


def _limit(raw: str | None) -> int:
    """`limit` como entero; uno no entero se informa como `out_of_range` al validarlo."""
    if raw is None:
        return DEFAULT_LIMIT
    try:
        return int(raw.strip())
    except ValueError:
        return 0


def _invalid_routine(error: InvalidRoutine) -> Response:
    return error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        error.code,
        "La rutina no es válida.",
        [ErrorDetail(field=f"body.{item.field}", issue=item.issue) for item in error.violations],
    )


def _invalid_query(error: InvalidRoutineQuery) -> Response:
    return error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        error.code,
        "La consulta no es válida.",
        [ErrorDetail(field=f"query.{error.field}", issue=error.issue)],
    )


_AUTHOR_FORBIDDEN = {
    "model": ErrorResponse,
    "description": f"`permission_required` (`{NUTRITION_PLAN_AUTHOR_PERMISSION}`, solo "
    "profesional).",
}


def build_nutrition_routine_templates_router(
    *,
    verify_token: TokenVerifier,
    publish: PublishRoutine,
    retire: RetireRoutine,
    list_routines: ListMyRoutines,
    suggest: SuggestRoutines,
) -> APIRouter:
    router = APIRouter(tags=["nutrition-routine-templates"])

    @router.post(
        BASE,
        status_code=HTTPStatus.CREATED,
        response_model=RoutineResponse,
        summary="Publicar una rutina alimenticia (HU030)",
        description="La rutina nace `PUBLISHED` en versión 1; no es un plan asignado ni "
        "incluye envío, compra o pago. Reintentar con la misma `Idempotency-Key` (24 horas) "
        "devuelve la misma rutina.",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.CREATED.value: {
                "headers": {
                    "Location": {
                        "description": f"`{BASE}/{{template_id}}`.",
                        "schema": {"type": "string"},
                    },
                    REPLAYED_HEADER: {
                        "description": "`true` cuando se devuelve el resultado de un reintento.",
                        "schema": {"type": "string"},
                    },
                }
            },
            HTTPStatus.FORBIDDEN.value: _AUTHOR_FORBIDDEN,
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`idempotency_key_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` con `details[{field, issue}]` por campo "
                "(longitudes, catálogos, días, comidas, porciones); "
                "`body.daily_meals.<i>.items.<j>.food_code` con `unknown_value` si el alimento "
                "no existe en el catálogo o `conflicts_with_restriction` si es incompatible "
                "con el `dietary_pattern` declarado; `body.contains_allergens` con "
                "`duplicate_value`. Nada se publica.",
            },
        },
    )
    def publish_routine(
        body: PublishRoutineRequest,
        response: Response,
        idempotency_key: Annotated[UUID, Header(alias=IDEMPOTENCY_HEADER)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | RoutineResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            result = publish(caller, _publish_command(body, idempotency_key))
        except PermissionRequired as error:
            return forbidden(error)
        except InvalidRoutine as error:
            return _invalid_routine(error)
        except IdempotencyKeyReused as error:
            return error_response(
                HTTPStatus.CONFLICT,
                error.code,
                "La clave de idempotencia ya se usó con otra solicitud.",
            )
        response.headers["Location"] = f"{BASE}/{result.routine.template_id}"
        if result.replayed:
            response.headers[REPLAYED_HEADER] = "true"
        return RoutineResponse.from_routine(result.routine)

    @router.post(
        f"{BASE}/{{template_id}}/retire",
        response_model=RoutineResponse,
        summary="Retirar una rutina alimenticia propia (HU030)",
        description="`PUBLISHED` → `RETIRED` con la versión siguiente; la rutina se conserva y "
        "deja de sugerirse. Una rutina ya retirada se devuelve tal cual (retiro idempotente).",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.FORBIDDEN.value: _AUTHOR_FORBIDDEN,
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": "`template_not_found`: no existe o es de otro profesional.",
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`version_conflict`: `details[0]` = `{field: expected_version, "
                "issue: <versión vigente>}`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `body.expected_version`.",
            },
        },
    )
    def retire_routine(
        template_id: UUID,
        body: RetireRoutineRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | RoutineResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            result = retire(
                caller,
                RetireRoutineCommand(
                    template_id=template_id,
                    expected_version=body.expected_version,
                    correlation_id=correlation_uuid(),
                ),
            )
        except PermissionRequired as error:
            return forbidden(error)
        except RoutineNotFound as error:
            return error_response(HTTPStatus.NOT_FOUND, error.code, "La rutina no existe.")
        except RoutineVersionConflict as error:
            return error_response(
                HTTPStatus.CONFLICT,
                error.code,
                "La rutina cambió desde la versión indicada.",
                [ErrorDetail(field="expected_version", issue=str(error.current_version))],
            )
        return RoutineResponse.from_routine(result.routine)

    @router.get(
        "/v1/me/nutrition-routine-templates",
        response_model=RoutinesResponse,
        summary="Mis rutinas alimenticias (HU030, lectura CQRS)",
        description="Rutinas publicadas y retiradas del profesional autenticado, orden "
        "`created_at` desc y `template_id` desc.",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.FORBIDDEN.value: _AUTHOR_FORBIDDEN,
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `query.limit` (`out_of_range`, también si "
                "no es un entero) o `query.cursor` (`invalid_cursor`).",
            },
        },
    )
    def my_routines(
        *,
        response: Response,
        limit: Annotated[
            str | None,
            Query(description=f"Entero de 1 a {MAX_LIMIT}; {DEFAULT_LIMIT} por defecto."),
        ] = None,
        cursor: Annotated[str | None, Query(description="`next_cursor` anterior.")] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | RoutinesResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            page = list_routines(caller, MyRoutinesFilters(limit=_limit(limit), cursor=cursor))
        except PermissionRequired as error:
            return forbidden(error)
        except InvalidRoutineQuery as error:
            return _invalid_query(error)
        response.headers["Cache-Control"] = "no-store"
        return RoutinesResponse.from_page(page)

    @router.get(
        "/v1/me/suggestions/nutrition-routines",
        response_model=RoutineSuggestionsResponse,
        summary="Rutinas alimenticias sugeridas para el deportista (HU030, lectura CQRS)",
        description="Con `dietary_pattern`: rutinas `PUBLISHED` sin los alérgenos indicados y "
        "con un patrón apto para el del deportista (las demás se excluyen), primero las de su "
        "objetivo, luego las de sus deportes y después por `published_at` y `template_id` "
        "descendentes, con `reasons`. Sin `dietary_pattern`: las últimas publicadas, "
        "`personalized: false` y sin motivos. Los filtros los envía `bff-mobile` con los datos "
        "de Usuarios; nunca el cliente.",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` (no es deportista) o `capability_required` "
                f"(`{ROUTINE_SUGGEST_CAPABILITY}`).",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `query.limit` (`out_of_range`), "
                "`query.exclude_allergens` o `query.sport` (valor desconocido o "
                "`duplicate_value`), `query.dietary_pattern` o `query.primary_goal` "
                "(desconocido) y `query.dietary_pattern` `required` si llegan alergias sin "
                "patrón.",
            },
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": "`upstream_unavailable`: no se pudo verificar identidad o plan.",
            },
            HTTPStatus.GATEWAY_TIMEOUT.value: {
                "model": ErrorResponse,
                "description": "`upstream_timeout`: Afiliación no respondió a tiempo.",
            },
        },
    )
    def routine_suggestions(
        *,
        response: Response,
        dietary_pattern: Annotated[
            DietPattern | None, Query(description="Patrón alimenticio del deportista (HU009).")
        ] = None,
        exclude_allergens: Annotated[
            list[Allergy] | None, Query(description="Alergias del deportista (HU009); repetible.")
        ] = None,
        sport: Annotated[
            list[Sport] | None, Query(description="Deportes del deportista (HU006); repetible.")
        ] = None,
        primary_goal: Annotated[
            Goal | None, Query(description="Objetivo principal del deportista (HU006).")
        ] = None,
        limit: Annotated[
            str | None,
            Query(description=f"Entero de 1 a {MAX_LIMIT}; {DEFAULT_LIMIT} por defecto."),
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | RoutineSuggestionsResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        filters = RoutineSuggestionFilters(
            dietary_pattern=dietary_pattern,
            exclude_allergens=tuple(exclude_allergens or ()),
            sports=tuple(sport or ()),
            primary_goal=primary_goal,
            limit=_limit(limit),
        )
        try:
            result = suggest(caller, filters)
        except InvalidRoutineQuery as error:
            return _invalid_query(error)
        except (PermissionRequired, CapabilityRequired) as error:
            return forbidden(error)
        except CapabilitiesTimeout as error:
            return error_response(
                HTTPStatus.GATEWAY_TIMEOUT,
                error.code,
                "No fue posible verificar su plan a tiempo. Intente de nuevo.",
            )
        except CapabilitiesUnavailable as error:
            return error_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                error.code,
                "No fue posible verificar su plan. Intente de nuevo.",
            )
        response.headers["Cache-Control"] = "no-store"
        return RoutineSuggestionsResponse.from_result(result)

    return router
