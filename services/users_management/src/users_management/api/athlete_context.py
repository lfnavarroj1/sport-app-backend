"""Contrato interno `GET /v1/users/{user_id}/athlete-context` (ADR-041, HU026).

Lo llama `bff-mobile` dentro de la red de la plataforma, sin token, como
`GET /v1/users/{user_id}/nutrition-restrictions`. Devuelve únicamente `sports`,
`primary_goal`, `city`, `country_code` y `birth_date`; ninguna otra sección del perfil.

Ningún cliente debe llegar aquí: `bff-mobile` no expone la ruta y nunca reenvía el token.
Si una solicitud trae `Authorization` (la ruta quedó expuesta por error), se responde
`403 permission_required` sin leer datos y se audita como rechazada (mismo criterio que el
contrato de restricciones de HU009).
"""

from datetime import date
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Response
from pydantic import BaseModel, Field

from sportapp_platform.errors import ErrorResponse, error_response
from users_management.api.identity import PERMISSION_REQUIRED
from users_management.api.lookup import USER_NOT_FOUND
from users_management.api.nutrition_restrictions import AUTHORIZATION_PRESENT
from users_management.application.errors import ReadModelUnavailable
from users_management.application.queries.athlete_context import GetAthleteContext
from users_management.domain.profile.sport_habits import PrimaryGoal, Sport


class AthleteContextResponse(BaseModel):
    sports: Annotated[
        list[Sport],
        Field(description="Deportes de HU006 en el orden del catálogo; vacío sin hábitos."),
    ]
    primary_goal: PrimaryGoal | None = Field(
        description="Objetivo principal de HU006; `null` si no lo registró."
    )
    city: str | None = Field(description="Ciudad de HU005; `null` sin perfil demográfico.")
    country_code: str | None = Field(
        description="ISO 3166-1 alfa-2 de HU005; `null` sin perfil demográfico."
    )
    birth_date: date | None = Field(
        description="Fecha de nacimiento de HU005; `null` sin perfil demográfico."
    )


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def build_athlete_context_router(query: GetAthleteContext) -> APIRouter:
    router = APIRouter(prefix="/v1/users", tags=["users"])

    @router.get(
        "/{user_id}/athlete-context",
        response_model=AthleteContextResponse,
        summary="Contexto del deportista para sugerencias (interno, ADR-041)",
        description="Solo para `bff-mobile` (HU026), dentro de la red de la plataforma y sin "
        "token. Una cuenta activa sin perfil responde con `sports` vacío y los demás campos "
        "en `null`. Datos personales: `no-store`.",
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
    def athlete_context(
        user_id: UUID,
        response: Response,
        authorization: Annotated[
            str | None,
            Header(description="No se envía: con ella la solicitud se rechaza (`403`)."),
        ] = None,
    ) -> Response | AthleteContextResponse:
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
                    "No fue posible consultar el contexto. Intente de nuevo.",
                )
            )
        if found is None:
            return _no_store(
                error_response(HTTPStatus.NOT_FOUND, USER_NOT_FOUND, "No existe una cuenta activa.")
            )
        _no_store(response)
        return AthleteContextResponse.model_validate(
            {
                "sports": list(found.sports),
                "primary_goal": found.primary_goal,
                "city": found.city,
                "country_code": found.country_code,
                "birth_date": found.birth_date,
            }
        )

    return router
