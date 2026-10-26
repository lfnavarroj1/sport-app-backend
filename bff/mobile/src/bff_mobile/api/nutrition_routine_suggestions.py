"""HU030 en el canal móvil: rutinas de alimentación sugeridas (ADR-041).

Mismo recorrido que las plantillas de HU028 (`api/template_suggestions.py`), con la misma caché
del contexto del deportista:
1. Borde: token, actor `athlete` y capacidad vigente `nutrition.routines.suggest` (Afiliación).
2. Contexto: `GET /v1/users/{user_id}/athlete-context` (timeout 300 ms, caché de 5 minutos).
3. Restricciones: `GET /v1/users/{user_id}/nutrition-restrictions` (contrato interno de HU009,
   timeout 300 ms, **sin caché**: son datos de salud y solo transitan en la solicitud; nunca se
   registran ni se devuelven al cliente). Se conocen solo si el deportista registró hábitos
   alimenticios (`version` no nula). Si no los registró o Usuarios no responde, la lista se
   entrega **no personalizada** con `missing_context: ["nutrition_habits", ...]` y el aviso
   `RESTRICTIONS_NOT_VERIFIED`; nunca como compatible con sus restricciones.
4. Dominio: `training_management` recibe patrón, alérgenos a excluir, deportes y objetivo como
   filtros explícitos y aplica las reglas. Si falla, `503`/`504`.
5. Autoría: `author_name` por `POST /v1/users/directory-lookup`; sin respuesta, `null`.
Sugerir no asigna la rutina ni reemplaza una valoración médica (`NOT_MEDICAL_ADVICE`).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bff_mobile.api.route_suggestions import (
    CONTEXT_TIMEOUT_SECONDS,
    AthleteContext,
    AthleteContextCache,
    Sport,
    SuggestionSource,
    _athlete,
    _athlete_context,
    _author_names,
    _capability_denied,
)
from bff_mobile.api.template_suggestions import Goal
from bff_mobile.api.training_metrics import _failure, _passthrough
from bff_mobile.clients.training_management import TrainingManagementClient
from bff_mobile.clients.users_management import (
    UpstreamTimeout,
    UpstreamUnavailable,
    UsersManagementClient,
)
from sportapp_platform.errors import ErrorResponse
from sportapp_platform.tokens import VerifiedAccessToken

ROUTINES_CAPABILITY = "nutrition.routines.suggest"
DEFAULT_LIMIT = 20
MAX_LIMIT = 50

DietPattern = Literal["OMNIVORE", "VEGETARIAN", "VEGAN", "PESCATARIAN", "FLEXITARIAN"]
Allergy = Literal[
    "PEANUTS", "TREE_NUTS", "MILK", "EGGS", "WHEAT", "SOY", "FISH", "SHELLFISH", "SESAME"
]
MissingRoutineContext = Literal["nutrition_habits", "sports", "primary_goal"]
Notice = Literal["RESTRICTIONS_NOT_VERIFIED", "NOT_MEDICAL_ADVICE"]


class NutritionRestrictions(BaseModel):
    """Lo que las sugerencias usan del contrato interno de restricciones de Usuarios."""

    diet_pattern: str | None
    allergies: list[str]
    version: int | None


@dataclass(frozen=True)
class RestrictionsLookup:
    """Restricciones conocidas (o `None`) y si Usuarios respondió (un `404` es respuesta)."""

    known: NutritionRestrictions | None
    responded: bool


class RoutineItem(BaseModel):
    food_code: str
    portions: int


class RoutineMeal(BaseModel):
    meal: Literal["BREAKFAST", "LUNCH", "DINNER", "SNACK", "HYDRATION"]
    items: list[RoutineItem]


class RoutineReference(BaseModel):
    type: Literal["nutrition_routine_template"]
    id: UUID


class RoutineSuggestionItem(BaseModel):
    reference: RoutineReference
    title: Annotated[str, Field(description="Título de la rutina.")]
    summary: Annotated[str, Field(description="Descripción de la rutina.")]
    reasons: list[Literal["RESTRICTION_COMPATIBLE", "GOAL_MATCH", "SPORT_MATCH"]] = Field(
        description="Motivos de la sugerencia; vacío en la lista no personalizada."
    )
    template_id: UUID
    professional_id: Annotated[UUID, Field(description="Profesional autor de la rutina.")]
    goal: Goal
    sport: Sport | None
    dietary_pattern: DietPattern
    contains_allergens: Annotated[
        list[Allergy], Field(description="Alérgenos de la rutina, en el orden del catálogo.")
    ]
    duration_days: int
    daily_meals: list[RoutineMeal]
    published_at: Annotated[datetime, Field(description="Instante en UTC con sufijo `Z`.")]
    author_name: Annotated[
        str | None,
        Field(description="Nombre del profesional; `null` si Usuarios no respondió."),
    ]


class RoutineSuggestionsResponse(BaseModel):
    items: list[RoutineSuggestionItem]
    personalized: bool = Field(
        description="`false` sin hábitos alimenticios registrados (HU009) o si Usuarios no "
        "respondió: últimas rutinas publicadas, sin motivos y sin verificar restricciones."
    )
    missing_context: list[MissingRoutineContext] = Field(
        description="Datos del perfil que faltan: `nutrition_habits` (HU009, también si Usuarios "
        "no respondió), `sports` y `primary_goal` (HU006; ambos si Usuarios no respondió)."
    )
    notices: list[Notice] = Field(
        description="Avisos fijos que el cliente localiza: `RESTRICTIONS_NOT_VERIFIED` cuando la "
        "lista no está personalizada (no se comprobaron alergias ni patrón alimenticio) y "
        "`NOT_MEDICAL_ADVICE` siempre (la sugerencia no reemplaza una valoración médica)."
    )
    generated_at: Annotated[datetime, Field(description="Instante en UTC con sufijo `Z`.")]
    sources: list[SuggestionSource]


def _restrictions(users: UsersManagementClient, user_id: UUID) -> RestrictionsLookup:
    """Restricciones vigentes del deportista; sin caché (datos de salud)."""
    try:
        found = users.nutrition_restrictions(user_id, timeout=CONTEXT_TIMEOUT_SECONDS)
        restrictions = None if found is None else NutritionRestrictions.model_validate(found)
    except (UpstreamTimeout, UpstreamUnavailable, ValueError):
        return RestrictionsLookup(known=None, responded=False)
    if restrictions is None or restrictions.version is None or restrictions.diet_pattern is None:
        # Sin cuenta activa resoluble o sin hábitos de HU009: restricciones desconocidas.
        return RestrictionsLookup(known=None, responded=True)
    return RestrictionsLookup(known=restrictions, responded=True)


def _missing(
    context: AthleteContext | None, restrictions: NutritionRestrictions | None
) -> list[MissingRoutineContext]:
    missing: list[MissingRoutineContext] = []
    if restrictions is None:
        missing.append("nutrition_habits")
    if context is None:
        return [*missing, "sports", "primary_goal"]
    if not context.sports:
        missing.append("sports")
    if context.primary_goal is None:
        missing.append("primary_goal")
    return missing


def _params(
    context: AthleteContext | None,
    restrictions: NutritionRestrictions | None,
    limit: str | None,
) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = []
    if restrictions is not None and restrictions.diet_pattern is not None:
        params.append(("dietary_pattern", restrictions.diet_pattern))
        params += [("exclude_allergens", allergy) for allergy in restrictions.allergies]
    if context is not None:
        params += [("sport", sport) for sport in context.sports]
        if context.primary_goal is not None:
            params.append(("primary_goal", context.primary_goal))
    if limit is not None:
        params.append(("limit", limit))
    return params


def _item(routine: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    return {
        "reference": {"type": "nutrition_routine_template", "id": routine["template_id"]},
        "title": routine["title"],
        "summary": routine["description"],
        "reasons": routine["reasons"],
        **{
            key: routine[key]
            for key in (
                "template_id",
                "professional_id",
                "goal",
                "sport",
                "dietary_pattern",
                "contains_allergens",
                "duration_days",
                "daily_meals",
                "published_at",
            )
        },
        "author_name": names.get(routine["professional_id"]),
    }


def _compose(
    body: dict[str, Any],
    context: AthleteContext | None,
    restrictions: RestrictionsLookup,
    author_names: Callable[[list[dict[str, Any]]], dict[str, str] | None],
) -> Response:
    routines: list[dict[str, Any]] = body["items"]
    names = author_names(routines)
    users_ok = context is not None and restrictions.responded and names is not None
    personalized = bool(body["personalized"])
    notices: list[Notice] = [] if personalized else ["RESTRICTIONS_NOT_VERIFIED"]
    document = RoutineSuggestionsResponse.model_validate(
        {
            "items": [_item(routine, names or {}) for routine in routines],
            "personalized": personalized,
            "missing_context": _missing(context, restrictions.known),
            "notices": [*notices, "NOT_MEDICAL_ADVICE"],
            "generated_at": datetime.now(UTC).replace(microsecond=0),
            "sources": [{"source": "users", "status": "OK" if users_ok else "UNAVAILABLE"}],
        }
    )
    return JSONResponse(content=document.model_dump(mode="json"))


def _relay(
    upstream: Any,
    context: AthleteContext | None,
    restrictions: RestrictionsLookup,
    author_names: Callable[[list[dict[str, Any]]], dict[str, str] | None],
) -> Response:
    if upstream.status_code != HTTPStatus.OK:
        return _passthrough(
            upstream,
            {
                HTTPStatus.FORBIDDEN,
                HTTPStatus.UNPROCESSABLE_ENTITY,
                HTTPStatus.SERVICE_UNAVAILABLE,
                HTTPStatus.GATEWAY_TIMEOUT,
            },
        )
    try:
        return _compose(upstream.json(), context, restrictions, author_names)
    except (KeyError, TypeError, ValueError):
        # Respuesta de Entrenamientos fuera de su contrato.
        return _failure(UpstreamUnavailable())


def build_nutrition_routine_suggestions_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    athlete_snapshot: Callable[[UUID], dict[str, Any]],
    users: UsersManagementClient,
    training: TrainingManagementClient,
    cache: AthleteContextCache | None = None,
) -> APIRouter:
    router = APIRouter(tags=["suggestions"])
    contexts = cache or AthleteContextCache()

    @router.get(
        "/v1/me/suggestions/nutrition-routines",
        response_model=RoutineSuggestionsResponse,
        summary="Rutinas de alimentación sugeridas (HU030)",
        description="Rutinas publicadas por profesionales compatibles con el patrón alimenticio "
        "y sin los alérgenos del deportista (HU009): primero las de su objetivo, luego las de "
        "sus deportes y las más recientes, con `reasons`. Sin hábitos alimenticios registrados "
        "o sin respuesta de Usuarios: las últimas publicadas, `personalized: false`, "
        "`missing_context` y el aviso `RESTRICTIONS_NOT_VERIFIED`. Una rutina retirada no se "
        "sugiere. Sugerir no asigna la rutina ni reemplaza una valoración médica.",
        responses={
            HTTPStatus.UNAUTHORIZED.value: {
                "model": ErrorResponse,
                "description": "`invalid_access_token`: falta el token o no es válido.",
            },
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` (no es deportista) o `capability_required` "
                f"(`{ROUTINES_CAPABILITY}`).",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `query.limit` (`out_of_range`, también si "
                "no es un entero).",
            },
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": "`upstream_unavailable`: plan no verificable o Entrenamientos "
                "caído (si solo falla Usuarios, la lista se entrega sin personalizar).",
            },
            HTTPStatus.GATEWAY_TIMEOUT.value: {
                "model": ErrorResponse,
                "description": "`upstream_timeout`: plan o Entrenamientos sin respuesta.",
            },
        },
    )
    def nutrition_routine_suggestions(
        *,
        limit: Annotated[
            str | None,
            Query(description=f"Entero de 1 a {MAX_LIMIT}; {DEFAULT_LIMIT} por defecto."),
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        caller = _athlete(verify_token, authorization)
        if not isinstance(caller, Response):
            user_id, token = caller
            caller = _capability_denied(athlete_snapshot, user_id, ROUTINES_CAPABILITY) or caller
        if isinstance(caller, Response):
            caller.headers["Cache-Control"] = "no-store"
            return caller
        context = _athlete_context(users, contexts, user_id)
        restrictions = _restrictions(users, user_id)
        try:
            upstream = training.nutrition_routine_suggestions(
                token, _params(context, restrictions.known, limit)
            )
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            response = _failure(error)
        else:
            response = _relay(
                upstream,
                context,
                restrictions,
                lambda routines: _author_names(users, routines, "professional_id"),
            )
        response.headers["Cache-Control"] = "no-store"
        return response

    return router
