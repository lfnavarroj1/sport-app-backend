"""Contrato interno `GET /v1/users/{user_id}/nutrition-restrictions` (ADR-035).

Lo llama `training-management` dentro de la red de la plataforma, sin token, como
`POST /v1/users/lookup`. Devuelve únicamente `restrictions`, `allergies`, `diet_pattern` y
`version`; ninguna otra sección del perfil. Desde HU009 los valores son los hábitos
alimenticios proyectados.

Ningún cliente debe llegar aquí: los clientes siempre envían su access token y `bff-web`
no expone la ruta. Si una solicitud trae `Authorization` (la ruta quedó expuesta por error),
se responde `403 permission_required` sin leer datos y se audita como rechazada (decisión
del 2026-10-24).
"""

from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Response
from pydantic import BaseModel, Field

from sportapp_platform.errors import ErrorResponse, error_response
from users_management.api.identity import PERMISSION_REQUIRED
from users_management.api.lookup import USER_NOT_FOUND
from users_management.application.errors import ReadModelUnavailable
from users_management.application.queries.nutrition_restrictions import (
    GetNutritionRestrictions,
)
from users_management.domain.profile.nutrition_habits import Allergy, DietPattern, Restriction

# Motivo de auditoría: la solicitud traía `Authorization` (un cliente).
AUTHORIZATION_PRESENT = "authorization_present"


class NutritionRestrictionsResponse(BaseModel):
    restrictions: Annotated[list[Restriction], Field(description="En el orden del catálogo.")]
    allergies: Annotated[list[Allergy], Field(description="En el orden del catálogo.")]
    diet_pattern: DietPattern | None = Field(
        description="`null` si el usuario no registró hábitos alimenticios."
    )
    version: int | None = Field(
        description="Versión de los hábitos alimenticios proyectada; `null` si no los registró "
        "(en ese caso las listas van vacías)."
    )


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def build_nutrition_restrictions_router(query: GetNutritionRestrictions) -> APIRouter:
    router = APIRouter(prefix="/v1/users", tags=["users"])

    @router.get(
        "/{user_id}/nutrition-restrictions",
        response_model=NutritionRestrictionsResponse,
        summary="Restricciones y alergias alimenticias de un usuario (interno, ADR-035)",
        description="Solo para `training-management` (HU014), dentro de la red de la "
        "plataforma y sin token. Sin hábitos registrados: listas vacías y "
        "`diet_pattern`/`version` en `null`. Datos de salud: `no-store`.",
        responses={
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": f"`{PERMISSION_REQUIRED}`: la solicitud trae `Authorization` "
                "(un cliente no puede usar este contrato interno).",
            },
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": f"`{USER_NOT_FOUND}`: no hay una cuenta activa con ese "
                "identificador.",
            },
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": f"`{ReadModelUnavailable.code}`: la base de lectura no responde.",
            },
        },
    )
    def nutrition_restrictions(
        user_id: UUID,
        response: Response,
        authorization: Annotated[
            str | None,
            Header(description="No se envía: con ella la solicitud se rechaza (`403`)."),
        ] = None,
    ) -> Response | NutritionRestrictionsResponse:
        if authorization is not None:
            query.reject(user_id, AUTHORIZATION_PRESENT)
            return _no_store(
                error_response(
                    HTTPStatus.FORBIDDEN,
                    PERMISSION_REQUIRED,
                    "Esta operación no está disponible para los clientes.",
                )
            )
        try:
            found = query(user_id)
        except ReadModelUnavailable as error:
            return _no_store(
                error_response(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    error.code,
                    "No fue posible consultar las restricciones. Intente de nuevo.",
                )
            )
        if found is None:
            return _no_store(
                error_response(HTTPStatus.NOT_FOUND, USER_NOT_FOUND, "No existe una cuenta activa.")
            )
        _no_store(response)
        return NutritionRestrictionsResponse.model_validate(
            {
                "restrictions": list(found.restrictions),
                "allergies": list(found.allergies),
                "diet_pattern": found.diet_pattern,
                "version": found.version,
            }
        )

    return router
