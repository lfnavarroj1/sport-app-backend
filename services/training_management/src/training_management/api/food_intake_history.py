"""HU013 (ADR-037): `GET /v1/me/food-intakes` y `GET /v1/athletes/{athlete_id}/food-intakes`.

El primero devuelve los consumos propios del deportista autenticado; el segundo, los de un
deportista que autorizó al profesional autenticado (`nutrition.history.view_assigned`). Ambos
leen `food_intake_view` (HU043) y paginan por cursor opaco. Cada consumo trae el mismo
esquema que devuelve `POST /v1/me/food-intakes`; sin calorías ni nutrientes.
"""

from datetime import date
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, Field

from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from training_management.api.food_intakes import FoodIntakeResponse
from training_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    forbidden,
)
from training_management.application.errors import (
    AthleteAuthorizationRequired,
    PermissionRequired,
)
from training_management.application.queries.food_intakes import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    FoodIntakeFilters,
    FoodIntakePage,
    GetFoodIntakeHistory,
    InvalidFoodIntakeQuery,
)
from training_management.domain.nutrition.model import MealType

DATE_FROM = "Primer día del periodo (inclusivo, fecha de consumo en UTC)."
DATE_TO = "Último día del periodo (inclusivo, fecha de consumo en UTC); no anterior a `from`."


class FoodIntakesPage(BaseModel):
    items: list[FoodIntakeResponse] = Field(
        description="Consumos de `consumed_at` y `intake_id` descendentes; vacía si no hay."
    )
    next_cursor: str | None = Field(
        description="Cursor opaco de la página siguiente; `null` si no hay más."
    )

    @classmethod
    def from_page(cls, page: FoodIntakePage) -> "FoodIntakesPage":
        return cls(
            items=[FoodIntakeResponse.from_intake(item) for item in page.items],
            next_cursor=page.next_cursor,
        )


_RESPONSES = {
    **AUTH_RESPONSES,
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required` o `athlete_authorization_required` (HU013 CA3).",
    },
    HTTPStatus.UNPROCESSABLE_ENTITY.value: {
        "model": ErrorResponse,
        "description": "`validation_error`: periodo (`query.to` `to_before_from`), límite o "
        "cursor (`query.cursor` `invalid_cursor`) inválidos (HU013 CA2).",
    },
}


def _invalid(error: InvalidFoodIntakeQuery) -> Response:
    return error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        error.code,
        "La consulta no es válida.",
        [ErrorDetail(field=f"query.{error.field}", issue=error.issue)],
    )


def build_food_intake_history_router(
    *, verify_token: TokenVerifier, get_intakes: GetFoodIntakeHistory
) -> APIRouter:
    router = APIRouter(tags=["food-intakes"])

    def respond(page: FoodIntakePage, response: Response) -> FoodIntakesPage:
        response.headers["Cache-Control"] = "no-store"
        return FoodIntakesPage.from_page(page)

    @router.get(
        "/v1/me/food-intakes",
        response_model=FoodIntakesPage,
        summary="Mi historial de alimentación (HU013, ADR-037)",
        responses=_RESPONSES,
    )
    def own_intakes(
        *,
        response: Response,
        date_from: Annotated[date | None, Query(alias="from", description=DATE_FROM)] = None,
        date_to: Annotated[date | None, Query(alias="to", description=DATE_TO)] = None,
        meal_type: MealType | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | FoodIntakesPage:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        filters = FoodIntakeFilters(date_from, date_to, meal_type, limit, cursor)
        try:
            page = get_intakes.own(caller, filters)
        except PermissionRequired as error:
            return forbidden(error)
        except InvalidFoodIntakeQuery as error:
            return _invalid(error)
        return respond(page, response)

    @router.get(
        "/v1/athletes/{athlete_id}/food-intakes",
        response_model=FoodIntakesPage,
        summary="Historial de alimentación de un deportista que me autorizó (HU013, ADR-037)",
        responses=_RESPONSES,
    )
    def athlete_intakes(
        *,
        athlete_id: UUID,
        response: Response,
        date_from: Annotated[date | None, Query(alias="from", description=DATE_FROM)] = None,
        date_to: Annotated[date | None, Query(alias="to", description=DATE_TO)] = None,
        meal_type: MealType | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | FoodIntakesPage:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        filters = FoodIntakeFilters(date_from, date_to, meal_type, limit, cursor)
        try:
            page = get_intakes.of_athlete(caller, athlete_id, filters)
        except (PermissionRequired, AthleteAuthorizationRequired) as error:
            return forbidden(error)
        except InvalidFoodIntakeQuery as error:
            return _invalid(error)
        return respond(page, response)

    return router
