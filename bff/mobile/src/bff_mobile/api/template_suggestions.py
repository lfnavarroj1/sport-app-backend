"""HU028 en el canal móvil: plantillas de plan de entrenamiento sugeridas (ADR-041).

Mismo recorrido que las rutas de HU026 (`api/route_suggestions.py`), con la misma caché del
contexto del deportista:
1. Borde: token, actor `athlete` y capacidad vigente `training.plans.suggest` (Afiliación).
2. Contexto: `GET /v1/users/{user_id}/athlete-context` (timeout 300 ms, caché de 5 minutos). Si
   Usuarios no responde, la lista se entrega **no personalizada** con `missing_context` y la
   fuente `users` en `UNAVAILABLE`.
3. Dominio: `training_management` recibe deportes y objetivo como filtros explícitos. Si falla,
   `503`/`504`.
4. Autoría: `author_name` por `POST /v1/users/directory-lookup`; sin respuesta, `null`.
Sugerir no asigna ni inicia el plan: «Autorizar a este profesional» es solo un enlace
informativo del cliente hacia la web (HU010).
"""

from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bff_mobile.api.route_suggestions import (
    AthleteContext,
    AthleteContextCache,
    Sport,
    SuggestionSource,
    _athlete,
    _athlete_context,
    _author_names,
    _capability_denied,
)
from bff_mobile.api.training_metrics import _failure, _passthrough
from bff_mobile.clients.training_management import TrainingManagementClient
from bff_mobile.clients.users_management import (
    UpstreamTimeout,
    UpstreamUnavailable,
    UsersManagementClient,
)
from sportapp_platform.errors import ErrorResponse
from sportapp_platform.tokens import VerifiedAccessToken

TEMPLATES_CAPABILITY = "training.plans.suggest"
DEFAULT_LIMIT = 20
MAX_LIMIT = 50

Goal = Literal["HEALTH", "PERFORMANCE", "WEIGHT_MANAGEMENT", "RECREATION", "COMPETITION"]
MissingTemplateContext = Literal["sports", "primary_goal"]


class WeeklySession(BaseModel):
    day_of_week: Literal["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    description: str
    duration_minutes: int


class TemplateReference(BaseModel):
    type: Literal["training_plan_template"]
    id: UUID


class TemplateSuggestionItem(BaseModel):
    reference: TemplateReference
    title: Annotated[str, Field(description="Título de la plantilla.")]
    summary: Annotated[str, Field(description="Objetivos de la plantilla.")]
    reasons: list[Literal["SPORT_MATCH", "GOAL_MATCH"]] = Field(
        description="Motivos de la sugerencia; vacío en la lista no personalizada."
    )
    template_id: UUID
    professional_id: Annotated[
        UUID, Field(description="Profesional autor (para el enlace informativo de HU010).")
    ]
    sport: Sport
    goal: Goal
    level: Literal["BEGINNER", "INTERMEDIATE", "ADVANCED"]
    duration_weeks: int
    weekly_sessions: list[WeeklySession]
    published_at: Annotated[datetime, Field(description="Instante en UTC con sufijo `Z`.")]
    author_name: Annotated[
        str | None,
        Field(description="Nombre del profesional; `null` si Usuarios no respondió."),
    ]


class TemplateSuggestionsResponse(BaseModel):
    items: list[TemplateSuggestionItem]
    personalized: bool = Field(
        description="`false` sin deportes registrados o si Usuarios no respondió: últimas "
        "plantillas publicadas, sin motivos."
    )
    missing_context: list[MissingTemplateContext] = Field(
        description="Datos del perfil que faltan (`sports` y `primary_goal` de HU006); ambos si "
        "Usuarios no respondió. Sin `primary_goal` la lista sigue personalizada por deporte."
    )
    generated_at: Annotated[datetime, Field(description="Instante en UTC con sufijo `Z`.")]
    sources: list[SuggestionSource]


def _missing(context: AthleteContext | None) -> list[MissingTemplateContext]:
    if context is None:
        return ["sports", "primary_goal"]
    missing: list[MissingTemplateContext] = []
    if not context.sports:
        missing.append("sports")
    if context.primary_goal is None:
        missing.append("primary_goal")
    return missing


def _params(context: AthleteContext | None, limit: str | None) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = []
    if context is not None:
        params += [("sport", sport) for sport in context.sports]
        if context.primary_goal is not None:
            params.append(("primary_goal", context.primary_goal))
    if limit is not None:
        params.append(("limit", limit))
    return params


def _item(template: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    return {
        "reference": {"type": "training_plan_template", "id": template["template_id"]},
        "title": template["title"],
        "summary": template["objectives"],
        "reasons": template["reasons"],
        **{
            key: template[key]
            for key in (
                "template_id",
                "professional_id",
                "sport",
                "goal",
                "level",
                "duration_weeks",
                "weekly_sessions",
                "published_at",
            )
        },
        "author_name": names.get(template["professional_id"]),
    }


def _compose(
    body: dict[str, Any],
    context: AthleteContext | None,
    author_names: Callable[[list[dict[str, Any]]], dict[str, str] | None],
) -> Response:
    templates: list[dict[str, Any]] = body["items"]
    names = author_names(templates)
    users_ok = context is not None and names is not None
    document = TemplateSuggestionsResponse.model_validate(
        {
            "items": [_item(template, names or {}) for template in templates],
            "personalized": bool(body["personalized"]),
            "missing_context": _missing(context),
            "generated_at": datetime.now(UTC).replace(microsecond=0),
            "sources": [{"source": "users", "status": "OK" if users_ok else "UNAVAILABLE"}],
        }
    )
    return JSONResponse(content=document.model_dump(mode="json"))


def _relay(
    upstream: Any,
    context: AthleteContext | None,
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
        return _compose(upstream.json(), context, author_names)
    except (KeyError, TypeError, ValueError):
        # Respuesta de Entrenamientos fuera de su contrato.
        return _failure(UpstreamUnavailable())


def build_template_suggestions_router(
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
        "/v1/me/suggestions/training-plan-templates",
        response_model=TemplateSuggestionsResponse,
        summary="Plantillas de plan sugeridas (HU028)",
        description="Plantillas publicadas por profesionales para los deportes del deportista: "
        "primero las de su objetivo, luego las más recientes, con `reasons`. Sin deportes "
        "registrados o sin respuesta de Usuarios: las últimas publicadas, `personalized: false` "
        "y `missing_context`. Una plantilla retirada no se sugiere. Sugerir no asigna ni inicia "
        "el plan.",
        responses={
            HTTPStatus.UNAUTHORIZED.value: {
                "model": ErrorResponse,
                "description": "`invalid_access_token`: falta el token o no es válido.",
            },
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` (no es deportista) o `capability_required` "
                f"(`{TEMPLATES_CAPABILITY}`).",
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
    def template_suggestions(
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
            caller = _capability_denied(athlete_snapshot, user_id, TEMPLATES_CAPABILITY) or caller
        if isinstance(caller, Response):
            caller.headers["Cache-Control"] = "no-store"
            return caller
        context = _athlete_context(users, contexts, user_id)
        try:
            upstream = training.training_plan_template_suggestions(token, _params(context, limit))
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            response = _failure(error)
        else:
            response = _relay(
                upstream,
                context,
                lambda templates: _author_names(users, templates, "professional_id"),
            )
        response.headers["Cache-Control"] = "no-store"
        return response

    return router
