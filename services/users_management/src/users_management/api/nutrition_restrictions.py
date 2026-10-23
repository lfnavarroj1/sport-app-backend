"""Contrato interno `GET /v1/users/{user_id}/nutrition-restrictions` (ADR-035).

Lo llama `training-management` dentro de la red de la plataforma, sin token, como
`POST /v1/users/lookup`. Devuelve únicamente `restrictions`, `allergies`, `diet_pattern` y
`version`; ninguna otra sección del perfil.
"""

from http import HTTPStatus
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from sportapp_platform.errors import ErrorResponse, error_response
from users_management.api.lookup import USER_NOT_FOUND
from users_management.application.queries.nutrition_restrictions import (
    GetNutritionRestrictions,
)

DietPattern = Literal["OMNIVORE", "VEGETARIAN", "VEGAN", "PESCATARIAN", "FLEXITARIAN"]
Restriction = Literal[
    "GLUTEN_FREE", "LACTOSE_FREE", "LOW_SODIUM", "LOW_SUGAR", "KETO", "HALAL", "KOSHER"
]
Allergy = Literal[
    "PEANUTS", "TREE_NUTS", "MILK", "EGGS", "WHEAT", "SOY", "FISH", "SHELLFISH", "SESAME"
]


class NutritionRestrictionsResponse(BaseModel):
    restrictions: list[Restriction]
    allergies: list[Allergy]
    diet_pattern: DietPattern | None = Field(
        description="`null` si el usuario no registró hábitos alimenticios."
    )
    version: int | None = Field(
        description="Versión de los hábitos alimenticios; `null` si no los registró (en ese "
        "caso las listas van vacías)."
    )


def build_nutrition_restrictions_router(query: GetNutritionRestrictions) -> APIRouter:
    router = APIRouter(prefix="/v1/users", tags=["users"])

    @router.get(
        "/{user_id}/nutrition-restrictions",
        response_model=NutritionRestrictionsResponse,
        summary="Restricciones y alergias alimenticias de un usuario (interno, ADR-035)",
        description="Solo para `training-management` (HU014). Sin hábitos registrados: listas "
        "vacías y `diet_pattern`/`version` en `null`.",
        responses={
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": f"`{USER_NOT_FOUND}`: no hay una cuenta activa con ese "
                "identificador.",
            },
        },
    )
    def nutrition_restrictions(
        user_id: UUID, response: Response
    ) -> Response | NutritionRestrictionsResponse:
        found = query(user_id)
        if found is None:
            return error_response(
                HTTPStatus.NOT_FOUND, USER_NOT_FOUND, "No existe una cuenta activa."
            )
        response.headers["Cache-Control"] = "no-store"
        return NutritionRestrictionsResponse.model_validate(
            {
                "restrictions": list(found.restrictions),
                "allergies": list(found.allergies),
                "diet_pattern": found.diet_pattern,
                "version": found.version,
            }
        )

    return router
