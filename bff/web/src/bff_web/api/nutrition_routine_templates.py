"""HU030 en el canal web: el profesional publica, retira y lista rutinas de alimentación
sugeribles (ADR-041).

El BFF autentica (JWKS), aplica el filtro de borde (`service_provider` con el permiso existente
`nutrition.plan.author`) y reenvía al servicio propietario con el token del usuario. Las reglas,
la coherencia con el catálogo de alimentos, la propiedad, la versión y la idempotencia viven en
`training_management`, que lo vuelve a verificar todo; el BFF entrega sus respuestas sin
alterarlas. Una rutina no es un plan de alimentación asignado (HU014): publicarla no asigna
nada. Sin envío a domicilio, compra, pago ni cálculo calórico.
"""

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from bff_web.api.nutrition import NUTRITION_AUTHOR_PERMISSION, MealType
from bff_web.api.training import (
    _UNAUTHORIZED,
    _UPSTREAM,
    IDEMPOTENCY_HEADER,
    REPLAYED_HEADER,
    _passthrough,
    _permission_required,
    _upstream_failure,
    authenticate_session,
)
from bff_web.api.training_plan_templates import Goal
from bff_web.api.training_routes import Sport
from bff_web.clients.training_management import TrainingManagementClient
from bff_web.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.authorization import ActorPermissions
from sportapp_platform.errors import ErrorResponse
from sportapp_platform.tokens import VerifiedAccessToken

BASE = "/v1/nutrition-routine-templates"
DEFAULT_LIMIT = 20
MAX_LIMIT = 50


# Enumeraciones con el mismo nombre y valores que el OpenAPI del servicio (prueba de contrato).
class DietPattern(StrEnum):
    """Catálogo `diet_pattern` de HU009 (ADR-035)."""

    OMNIVORE = "OMNIVORE"
    VEGETARIAN = "VEGETARIAN"
    VEGAN = "VEGAN"
    PESCATARIAN = "PESCATARIAN"
    FLEXITARIAN = "FLEXITARIAN"


class Allergy(StrEnum):
    """Catálogo de alergias de HU009 (ADR-035), en su orden."""

    PEANUTS = "PEANUTS"
    TREE_NUTS = "TREE_NUTS"
    MILK = "MILK"
    EGGS = "EGGS"
    WHEAT = "WHEAT"
    SOY = "SOY"
    FISH = "FISH"
    SHELLFISH = "SHELLFISH"
    SESAME = "SESAME"


class RoutineItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    food_code: Annotated[
        str,
        StringConstraints(min_length=1, max_length=64),
        Field(description="Código del catálogo de alimentos (`GET /v1/food-catalog`)."),
    ]
    portions: Annotated[int, Field(ge=1, le=10, strict=True)]


class RoutineMealRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meal: MealType
    items: Annotated[list[RoutineItemRequest], Field(min_length=1, max_length=15)]


# Mismo cuerpo que el servicio (prueba de contrato); las reglas las evalúa el servicio.
class PublishRoutineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=120)]
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
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
            max_length=9,
            description="Alérgenos que contiene (catálogo de HU009), sin repetir; puede ir "
            "vacía. El servicio agrega los alérgenos del catálogo de cada alimento.",
        ),
    ]
    duration_days: Annotated[int, Field(ge=1, le=90, strict=True)]
    daily_meals: Annotated[
        list[RoutineMealRequest],
        Field(
            min_length=1,
            max_length=8,
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
    contains_allergens: Annotated[
        list[Allergy], Field(description="En el orden del catálogo de HU009.")
    ]
    duration_days: Annotated[int, Field(description="Vigencia de la rutina en días.")]
    daily_meals: list[RoutineMealResponse]
    status: Literal["PUBLISHED", "RETIRED"]
    version: int
    created_at: datetime
    updated_at: datetime
    published_at: datetime
    retired_at: datetime | None


class RoutinesResponse(BaseModel):
    items: list[RoutineResponse]
    next_cursor: str | None = Field(
        description="Cursor opaco de la página siguiente; `null` si no hay más."
    )


def _error(status: HTTPStatus, description: str) -> dict[int | str, dict[str, Any]]:
    return {status.value: {"model": ErrorResponse, "description": description}}


_FORBIDDEN = _error(
    HTTPStatus.FORBIDDEN,
    f"`permission_required` (`{NUTRITION_AUTHOR_PERMISSION}`, solo profesional).",
)


def build_nutrition_routine_templates_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    permissions: ActorPermissions,
    training: TrainingManagementClient,
    channel: str,
) -> APIRouter:
    router = APIRouter(tags=["nutrition-routine-templates"])

    def author_token(authorization: str | None) -> str | Response:
        """Borde: token válido y permiso `nutrition.plan.author` en el canal."""
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        actor = session.actor_type
        if actor is None or NUTRITION_AUTHOR_PERMISSION not in permissions.for_actor(
            actor, channel
        ):
            return _permission_required()
        return session.token

    @router.post(
        BASE,
        status_code=HTTPStatus.CREATED,
        response_model=RoutineResponse,
        summary="Publicar una rutina de alimentación sugerible (HU030)",
        description="La rutina nace `PUBLISHED` en versión 1; no es un plan asignado. "
        "Reintentar con la misma `Idempotency-Key` (24 horas) devuelve la misma rutina.",
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
                "reglas del servicio, sin alterar: `unknown_value` si el alimento no está en el "
                "catálogo, `conflicts_with_restriction` si es incompatible con el patrón, "
                "`duplicate_value` en `body.contains_allergens`). Nada se publica.",
            ),
        },
    )
    def publish_routine(
        body: PublishRoutineRequest,
        idempotency_key: Annotated[UUID, Header(alias=IDEMPOTENCY_HEADER)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = author_token(authorization)
        if isinstance(token, Response):
            return token
        try:
            upstream = training.publish_nutrition_routine_template(
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
        response_model=RoutineResponse,
        summary="Retirar una rutina de alimentación propia (HU030)",
        description="`PUBLISHED` → `RETIRED` con la versión siguiente; la rutina se conserva y "
        "deja de sugerirse. Una rutina ya retirada se devuelve tal cual.",
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
    def retire_routine(
        template_id: UUID,
        body: RetireRoutineRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = author_token(authorization)
        if isinstance(token, Response):
            return token
        try:
            upstream = training.retire_nutrition_routine_template(
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
        "/v1/me/nutrition-routine-templates",
        response_model=RoutinesResponse,
        summary="Mis rutinas de alimentación (HU030, lectura CQRS)",
        description="Rutinas publicadas y retiradas del profesional, orden `created_at` y "
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
    def my_routines(
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
                upstream = training.list_my_nutrition_routine_templates(token, params)
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
