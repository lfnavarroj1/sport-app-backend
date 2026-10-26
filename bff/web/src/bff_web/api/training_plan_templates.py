"""HU028 en el canal web: el profesional publica, retira y lista plantillas de plan (ADR-041).

El BFF autentica (JWKS), aplica el filtro de borde (`service_provider` con el permiso existente
`training.plan.author`) y reenvía al servicio propietario con el token del usuario. Las reglas,
la propiedad, la versión y la idempotencia viven en `training_management`, que lo vuelve a
verificar todo; el BFF entrega sus respuestas sin alterarlas. Una plantilla no es un plan
asignado (HU010): publicarla no asigna ni inicia nada.
"""

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from bff_web.api.training import (
    _UNAUTHORIZED,
    _UPSTREAM,
    IDEMPOTENCY_HEADER,
    PLAN_AUTHOR_PERMISSION,
    REPLAYED_HEADER,
    _passthrough,
    _permission_required,
    _upstream_failure,
    authenticate_session,
)
from bff_web.api.training_routes import Sport
from bff_web.clients.training_management import TrainingManagementClient
from bff_web.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.authorization import ActorPermissions
from sportapp_platform.errors import ErrorResponse
from sportapp_platform.tokens import VerifiedAccessToken

BASE = "/v1/training-plan-templates"
DEFAULT_LIMIT = 20
MAX_LIMIT = 50


# Enumeraciones con el mismo nombre y valores que el OpenAPI del servicio (prueba de contrato).
class Goal(StrEnum):
    """Catálogo de objetivos de ADR-034 (`primary_goal` de HU006)."""

    HEALTH = "HEALTH"
    PERFORMANCE = "PERFORMANCE"
    WEIGHT_MANAGEMENT = "WEIGHT_MANAGEMENT"
    RECREATION = "RECREATION"
    COMPETITION = "COMPETITION"


class Level(StrEnum):
    BEGINNER = "BEGINNER"
    INTERMEDIATE = "INTERMEDIATE"
    ADVANCED = "ADVANCED"


class DayOfWeek(StrEnum):
    """Días de la disponibilidad de ADR-034."""

    MON = "MON"
    TUE = "TUE"
    WED = "WED"
    THU = "THU"
    FRI = "FRI"
    SAT = "SAT"
    SUN = "SUN"


class WeeklySessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day_of_week: DayOfWeek
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ]
    duration_minutes: Annotated[int, Field(ge=10, le=600, strict=True)]


# Mismo cuerpo que el servicio (prueba de contrato); las reglas las evalúa el servicio.
class PublishTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=120)]
    objectives: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=20, max_length=2000)
    ]
    sport: Annotated[Sport, Field(description="Catálogo de deportes de HU010.")]
    goal: Annotated[Goal, Field(description="Catálogo de objetivos de ADR-034.")]
    level: Level
    duration_weeks: Annotated[int, Field(ge=1, le=52, strict=True)]
    weekly_sessions: Annotated[
        list[WeeklySessionRequest],
        Field(
            min_length=1,
            max_length=14,
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


class TemplatesResponse(BaseModel):
    items: list[TemplateResponse]
    next_cursor: str | None = Field(
        description="Cursor opaco de la página siguiente; `null` si no hay más."
    )


def _error(status: HTTPStatus, description: str) -> dict[int | str, dict[str, Any]]:
    return {status.value: {"model": ErrorResponse, "description": description}}


_FORBIDDEN = _error(
    HTTPStatus.FORBIDDEN,
    f"`permission_required` (`{PLAN_AUTHOR_PERMISSION}`, solo profesional).",
)


def build_training_plan_templates_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    permissions: ActorPermissions,
    training: TrainingManagementClient,
    channel: str,
) -> APIRouter:
    router = APIRouter(tags=["training-plan-templates"])

    def author_token(authorization: str | None) -> str | Response:
        """Borde: token válido y permiso `training.plan.author` en el canal."""
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        actor = session.actor_type
        if actor is None or PLAN_AUTHOR_PERMISSION not in permissions.for_actor(actor, channel):
            return _permission_required()
        return session.token

    @router.post(
        BASE,
        status_code=HTTPStatus.CREATED,
        response_model=TemplateResponse,
        summary="Publicar una plantilla de plan de entrenamiento (HU028)",
        description="La plantilla nace `PUBLISHED` en versión 1; no es un plan asignado. "
        "Reintentar con la misma `Idempotency-Key` (24 horas) devuelve la misma plantilla.",
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
            **_FORBIDDEN,
            **_error(HTTPStatus.CONFLICT, "`idempotency_key_reused`."),
            **_error(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "`validation_error` con `details[{field, issue}]` por campo (forma del cuerpo y "
                "reglas del servicio, sin alterar). Nada se publica.",
            ),
        },
    )
    def publish_template(
        body: PublishTemplateRequest,
        idempotency_key: Annotated[UUID, Header(alias=IDEMPOTENCY_HEADER)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = author_token(authorization)
        if isinstance(token, Response):
            return token
        try:
            upstream = training.publish_training_plan_template(
                token, body.model_dump(mode="json"), idempotency_key
            )
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

    @router.post(
        f"{BASE}/{{template_id}}/retire",
        response_model=TemplateResponse,
        summary="Retirar una plantilla propia (HU028)",
        description="`PUBLISHED` → `RETIRED` con la versión siguiente; la plantilla se conserva "
        "y deja de sugerirse. Una plantilla ya retirada se devuelve tal cual.",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            **_FORBIDDEN,
            **_error(
                HTTPStatus.NOT_FOUND,
                "`template_not_found`: no existe o es de otro profesional.",
            ),
            **_error(
                HTTPStatus.CONFLICT,
                "`version_conflict`: `details[0]` = `{field: expected_version, issue: <versión "
                "vigente>}`.",
            ),
            **_error(
                HTTPStatus.UNPROCESSABLE_ENTITY, "`validation_error`: `body.expected_version`."
            ),
        },
    )
    def retire_template(
        template_id: UUID,
        body: RetireTemplateRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = author_token(authorization)
        if isinstance(token, Response):
            return token
        try:
            upstream = training.retire_training_plan_template(
                token, template_id, body.model_dump(mode="json")
            )
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        return _passthrough(
            upstream,
            {
                HTTPStatus.OK,
                HTTPStatus.FORBIDDEN,
                HTTPStatus.NOT_FOUND,
                HTTPStatus.CONFLICT,
                HTTPStatus.UNPROCESSABLE_ENTITY,
            },
        )

    @router.get(
        "/v1/me/training-plan-templates",
        response_model=TemplatesResponse,
        summary="Mis plantillas de plan (HU028, lectura CQRS)",
        description="Plantillas publicadas y retiradas del profesional, orden `created_at` y "
        "`template_id` descendente; el cursor es opaco.",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            **_FORBIDDEN,
            **_error(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "`validation_error` del servicio en `query.limit` (`out_of_range`, también si "
                "no es entero) o `query.cursor` (`invalid_cursor`), sin alterar.",
            ),
        },
    )
    def my_templates(
        *,
        limit: Annotated[
            str | None,
            Query(description=f"Entero de 1 a {MAX_LIMIT}; {DEFAULT_LIMIT} por defecto."),
        ] = None,
        cursor: Annotated[str | None, Query(description="`next_cursor` anterior.")] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = author_token(authorization)
        if isinstance(token, Response):
            response = token
        else:
            params = {
                name: value for name, value in (("limit", limit), ("cursor", cursor)) if value
            }
            try:
                upstream = training.list_my_training_plan_templates(token, params)
            except (UpstreamTimeout, UpstreamUnavailable) as error:
                response = _upstream_failure(error)
            else:
                response = _passthrough(
                    upstream,
                    {HTTPStatus.OK, HTTPStatus.FORBIDDEN, HTTPStatus.UNPROCESSABLE_ENTITY},
                )
        response.headers["Cache-Control"] = "no-store"
        return response

    return router
