"""HU028 (ADR-041): plantillas de plan de entrenamiento sugeribles.

- `POST /v1/training-plan-templates`: el `service_provider` con `training.plan.author` publica
  una plantilla (`201` + `Location`); la misma clave devuelve el resultado original con
  `Idempotent-Replayed: true`.
- `POST /v1/training-plan-templates/{template_id}/retire`: retira una plantilla propia con
  `expected_version`; una ajena o inexistente es `404 template_not_found`.
- `GET /v1/me/training-plan-templates`: plantillas del profesional autenticado (lectura CQRS).
- `GET /v1/me/suggestions/training-plan-templates`: sugerencias para el `athlete` con
  `training.plans.suggest`; `bff-mobile` envía los deportes y el objetivo como filtros.

El profesional sale del token. Una plantilla no es un plan asignado: sugerirla no asigna ni
inicia nada (HU010).
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
    PLAN_AUTHOR_PERMISSION,
    TEMPLATE_SUGGEST_CAPABILITY,
)
from training_management.application.commands.training_plan_templates import (
    PublishTemplate,
    PublishTemplateCommand,
    RetireTemplate,
    RetireTemplateCommand,
)
from training_management.application.errors import (
    CapabilitiesTimeout,
    CapabilitiesUnavailable,
    CapabilityRequired,
    IdempotencyKeyReused,
    PermissionRequired,
)
from training_management.application.queries.training_plan_templates import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    InvalidTemplateQuery,
    ListMyTemplates,
    MyTemplatesFilters,
    SuggestTemplates,
    TemplatePage,
    TemplateSuggestionFilters,
    TemplateSuggestions,
)
from training_management.domain.plans.model import Sport
from training_management.domain.templates.events import template_document
from training_management.domain.templates.model import (
    DESCRIPTION_MAX,
    DESCRIPTION_MIN,
    DURATION_WEEKS_MAX,
    DURATION_WEEKS_MIN,
    OBJECTIVES_MAX,
    OBJECTIVES_MIN,
    SESSION_MINUTES_MAX,
    SESSION_MINUTES_MIN,
    SESSIONS_MAX,
    SESSIONS_MIN,
    TITLE_MAX,
    TITLE_MIN,
    DayOfWeek,
    Goal,
    InvalidTemplate,
    Level,
    TemplateInput,
    TemplateNotFound,
    TemplateVersionConflict,
    TrainingPlanTemplate,
    WeeklySession,
)

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
BASE = "/v1/training-plan-templates"


class WeeklySessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day_of_week: DayOfWeek
    description: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=DESCRIPTION_MIN, max_length=DESCRIPTION_MAX
        ),
    ]
    duration_minutes: Annotated[
        int, Field(ge=SESSION_MINUTES_MIN, le=SESSION_MINUTES_MAX, strict=True)
    ]


class PublishTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=TITLE_MIN, max_length=TITLE_MAX)
    ]
    objectives: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=OBJECTIVES_MIN, max_length=OBJECTIVES_MAX
        ),
    ]
    sport: Annotated[Sport, Field(description="Catálogo de deportes de HU010.")]
    goal: Annotated[Goal, Field(description="Catálogo de objetivos de ADR-034.")]
    level: Level
    duration_weeks: Annotated[int, Field(ge=DURATION_WEEKS_MIN, le=DURATION_WEEKS_MAX, strict=True)]
    weekly_sessions: Annotated[
        list[WeeklySessionRequest],
        Field(
            min_length=SESSIONS_MIN,
            max_length=SESSIONS_MAX,
            description="Sesiones de una semana tipo, en el orden del profesional.",
        ),
    ]


class RetireTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[
        int, Field(ge=1, strict=True, description="Versión vigente que conoce el cliente.")
    ]


class WeeklySessionResponse(BaseModel):
    day_of_week: DayOfWeek
    description: str
    duration_minutes: int


class TemplateResponse(BaseModel):
    template_id: UUID
    professional_id: UUID
    title: str
    objectives: str
    sport: Sport
    goal: Goal
    level: Level
    duration_weeks: int
    weekly_sessions: list[WeeklySessionResponse]
    status: Literal["PUBLISHED", "RETIRED"]
    version: int
    created_at: datetime
    updated_at: datetime
    published_at: datetime
    retired_at: datetime | None

    @classmethod
    def from_template(cls, template: TrainingPlanTemplate) -> "TemplateResponse":
        return cls.model_validate(template_document(template))


class TemplatesResponse(BaseModel):
    items: list[TemplateResponse]
    next_cursor: str | None = Field(
        description="Cursor opaco de la página siguiente; `null` si no hay más."
    )

    @classmethod
    def from_page(cls, page: TemplatePage) -> "TemplatesResponse":
        return cls(
            items=[TemplateResponse.from_template(item) for item in page.items],
            next_cursor=page.next_cursor,
        )


class TemplateSuggestionResponse(TemplateResponse):
    reasons: list[Literal["SPORT_MATCH", "GOAL_MATCH"]] = Field(
        description="Motivos de la sugerencia; vacío en la lista no personalizada."
    )


class TemplateSuggestionsResponse(BaseModel):
    items: list[TemplateSuggestionResponse]
    personalized: bool = Field(
        description="`false` si no llegó ningún deporte: últimas publicadas, sin motivos."
    )

    @classmethod
    def from_result(cls, result: TemplateSuggestions) -> "TemplateSuggestionsResponse":
        return cls(
            items=[
                TemplateSuggestionResponse.model_validate(
                    {
                        **template_document(item.template),
                        "reasons": [reason.value for reason in item.reasons],
                    }
                )
                for item in result.items
            ],
            personalized=result.personalized,
        )


def _publish_command(body: PublishTemplateRequest, key: UUID) -> PublishTemplateCommand:
    return PublishTemplateCommand(
        data=TemplateInput(
            title=body.title,
            objectives=body.objectives,
            sport=body.sport,
            goal=body.goal,
            level=body.level,
            duration_weeks=body.duration_weeks,
            weekly_sessions=tuple(
                WeeklySession(
                    day_of_week=session.day_of_week,
                    description=session.description,
                    duration_minutes=session.duration_minutes,
                )
                for session in body.weekly_sessions
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


def _invalid_template(error: InvalidTemplate) -> Response:
    return error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        error.code,
        "La plantilla no es válida.",
        [ErrorDetail(field=f"body.{item.field}", issue=item.issue) for item in error.violations],
    )


def _invalid_query(error: InvalidTemplateQuery) -> Response:
    return error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        error.code,
        "La consulta no es válida.",
        [ErrorDetail(field=f"query.{error.field}", issue=error.issue)],
    )


_AUTHOR_FORBIDDEN = {
    "model": ErrorResponse,
    "description": f"`permission_required` (`{PLAN_AUTHOR_PERMISSION}`, solo profesional).",
}


def build_training_plan_templates_router(
    *,
    verify_token: TokenVerifier,
    publish: PublishTemplate,
    retire: RetireTemplate,
    list_templates: ListMyTemplates,
    suggest: SuggestTemplates,
) -> APIRouter:
    router = APIRouter(tags=["training-plan-templates"])

    @router.post(
        BASE,
        status_code=HTTPStatus.CREATED,
        response_model=TemplateResponse,
        summary="Publicar una plantilla de plan de entrenamiento (HU028)",
        description="La plantilla nace `PUBLISHED` en versión 1; no es un plan asignado. "
        "Reintentar con la misma `Idempotency-Key` (24 horas) devuelve la misma plantilla.",
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
                "(longitudes, catálogos, semanas, sesiones y minutos). Nada se publica.",
            },
        },
    )
    def publish_template(
        body: PublishTemplateRequest,
        response: Response,
        idempotency_key: Annotated[UUID, Header(alias=IDEMPOTENCY_HEADER)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | TemplateResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            result = publish(caller, _publish_command(body, idempotency_key))
        except PermissionRequired as error:
            return forbidden(error)
        except InvalidTemplate as error:
            return _invalid_template(error)
        except IdempotencyKeyReused as error:
            return error_response(
                HTTPStatus.CONFLICT,
                error.code,
                "La clave de idempotencia ya se usó con otra solicitud.",
            )
        response.headers["Location"] = f"{BASE}/{result.template.template_id}"
        if result.replayed:
            response.headers[REPLAYED_HEADER] = "true"
        return TemplateResponse.from_template(result.template)

    @router.post(
        f"{BASE}/{{template_id}}/retire",
        response_model=TemplateResponse,
        summary="Retirar una plantilla propia (HU028)",
        description="`PUBLISHED` → `RETIRED` con la versión siguiente; la plantilla se conserva "
        "y deja de sugerirse. Una plantilla ya retirada se devuelve tal cual.",
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
    def retire_template(
        template_id: UUID,
        body: RetireTemplateRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | TemplateResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            result = retire(
                caller,
                RetireTemplateCommand(
                    template_id=template_id,
                    expected_version=body.expected_version,
                    correlation_id=correlation_uuid(),
                ),
            )
        except PermissionRequired as error:
            return forbidden(error)
        except TemplateNotFound as error:
            return error_response(HTTPStatus.NOT_FOUND, error.code, "La plantilla no existe.")
        except TemplateVersionConflict as error:
            return error_response(
                HTTPStatus.CONFLICT,
                error.code,
                "La plantilla cambió desde la versión indicada.",
                [ErrorDetail(field="expected_version", issue=str(error.current_version))],
            )
        return TemplateResponse.from_template(result.template)

    @router.get(
        "/v1/me/training-plan-templates",
        response_model=TemplatesResponse,
        summary="Mis plantillas de plan (HU028, lectura CQRS)",
        description="Plantillas publicadas y retiradas del profesional autenticado, orden "
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
    def my_templates(
        *,
        response: Response,
        limit: Annotated[
            str | None,
            Query(description=f"Entero de 1 a {MAX_LIMIT}; {DEFAULT_LIMIT} por defecto."),
        ] = None,
        cursor: Annotated[str | None, Query(description="`next_cursor` anterior.")] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | TemplatesResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            page = list_templates(caller, MyTemplatesFilters(limit=_limit(limit), cursor=cursor))
        except PermissionRequired as error:
            return forbidden(error)
        except InvalidTemplateQuery as error:
            return _invalid_query(error)
        response.headers["Cache-Control"] = "no-store"
        return TemplatesResponse.from_page(page)

    @router.get(
        "/v1/me/suggestions/training-plan-templates",
        response_model=TemplateSuggestionsResponse,
        summary="Plantillas sugeridas para el deportista (HU028, lectura CQRS)",
        description="Plantillas `PUBLISHED` de los deportes indicados: primero las del "
        "objetivo (`primary_goal`), luego por `published_at` y `template_id` descendentes, con "
        "`reasons`. Sin `sport`: las últimas publicadas, `personalized: false` y sin motivos. "
        "Los filtros los envía `bff-mobile` con el contexto del deportista; nunca el cliente.",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` (no es deportista) o `capability_required` "
                f"(`{TEMPLATE_SUGGEST_CAPABILITY}`).",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `query.limit` (`out_of_range`), `query.sport` "
                "(valor desconocido o `duplicate_value`) o `query.primary_goal` (desconocido).",
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
    def template_suggestions(
        *,
        response: Response,
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
    ) -> Response | TemplateSuggestionsResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        filters = TemplateSuggestionFilters(
            sports=tuple(sport or ()), primary_goal=primary_goal, limit=_limit(limit)
        )
        try:
            result = suggest(caller, filters)
        except InvalidTemplateQuery as error:
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
        return TemplateSuggestionsResponse.from_result(result)

    return router
