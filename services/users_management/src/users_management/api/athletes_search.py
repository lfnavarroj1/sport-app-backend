"""Contrato interno `POST /v1/users/athletes/search` (ADR-045 §8, HU039).

Lo llama `eventos-notificaciones-worker` (`events-management`) dentro de la red de la
plataforma, sin token, como `GET /v1/users/{user_id}/athlete-context`. Devuelve solo
identificadores de deportistas activos; ningún dato del perfil.

El cuerpo se lee como JSON sin validación automática para responder con el catálogo de
errores del servicio (`details[{field, issue}]`, ADR-034) y para rechazar un token antes de
mirar el cuerpo; su esquema se documenta en el OpenAPI. Si una solicitud trae
`Authorization` (la ruta quedó expuesta por error), se responde `403 permission_required` sin
leer datos y se audita como rechazada (mismo criterio que HU009 y HU026). Todas las
respuestas llevan `Cache-Control: no-store`.
"""

from http import HTTPStatus
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from users_management.api.identity import PERMISSION_REQUIRED
from users_management.api.nutrition_restrictions import AUTHORIZATION_PRESENT
from users_management.api.profile import decode_body
from users_management.application.errors import ReadModelUnavailable
from users_management.application.queries.athletes_search import (
    CITY_MAX,
    CITY_MIN,
    CURSOR_MAX,
    DEFAULT_LIMIT,
    INVALID_CURSOR,
    LIMIT_MAX,
    LIMIT_MIN,
    SEARCHABLE_SPORTS,
    SPORTS_MAX,
    SPORTS_MIN,
    InvalidAthleteSearch,
    SearchAthletes,
)

INVALID_REQUEST_MESSAGE = "La solicitud no es válida."


class AthletesSearchRequest(BaseModel):
    """Solo documenta el esquema; la validación la hace `parse_athlete_search`."""

    model_config = ConfigDict(extra="forbid")

    sports: Annotated[
        list[str],
        Field(
            min_length=SPORTS_MIN,
            max_length=SPORTS_MAX,
            description="Entre 1 y 4 deportes únicos del catálogo de HU006; `other` no se "
            "acepta (no genera coincidencia, ADR-041).",
            json_schema_extra={"items": {"type": "string", "enum": list(SEARCHABLE_SPORTS)}},
        ),
    ]
    city: Annotated[
        str,
        Field(
            min_length=CITY_MIN,
            max_length=CITY_MAX,
            description="Ciudad del evento; se compara sin mayúsculas, acentos ni espacios "
            "exteriores.",
        ),
    ]
    country_code: Annotated[
        str,
        Field(
            min_length=2,
            max_length=2,
            description="ISO 3166-1 alfa-2 en mayúsculas; igualdad exacta.",
        ),
    ]
    limit: Annotated[
        int,
        Field(ge=LIMIT_MIN, le=LIMIT_MAX, description="Tamaño de la página (1 a 1.000)."),
    ] = DEFAULT_LIMIT
    cursor: Annotated[
        str | None,
        Field(
            max_length=CURSOR_MAX,
            description="`next_cursor` de la página anterior; `null` en la primera.",
        ),
    ] = None


class AthletesSearchResponse(BaseModel):
    user_ids: list[UUID] = Field(
        description="Deportistas activos que coinciden, en orden `user_id` ascendente."
    )
    next_cursor: str | None = Field(
        description="Cursor opaco de la siguiente página; `null` si es la última."
    )


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def _request_body() -> dict[str, Any]:
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": AthletesSearchRequest.model_json_schema()}},
        }
    }


def _search(query: SearchAthletes, raw_body: bytes) -> Response:
    try:
        page, next_cursor = query(decode_body(raw_body))
    except InvalidAthleteSearch as error:
        return error_response(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            error.code,
            INVALID_REQUEST_MESSAGE,
            [ErrorDetail(field=field, issue=issue) for field, issue in error.violations],
        )
    except ReadModelUnavailable as error:
        return error_response(
            HTTPStatus.SERVICE_UNAVAILABLE,
            error.code,
            "No fue posible buscar los deportistas. Intente de nuevo.",
        )
    return JSONResponse(
        content={
            "user_ids": [str(user_id) for user_id in page.user_ids],
            "next_cursor": next_cursor,
        }
    )


def build_athletes_search_router(query: SearchAthletes) -> APIRouter:
    router = APIRouter(prefix="/v1/users", tags=["users"])

    @router.post(
        "/athletes/search",
        response_model=AthletesSearchResponse,
        summary="Deportistas que coinciden por deporte, ciudad y país (interno, ADR-045)",
        description="Solo para `events-management` (HU039), dentro de la red de la "
        "plataforma y sin token. Coincide una cuenta activa de tipo `athlete` con alguno de "
        "los deportes en sus hábitos (HU006) y la misma ciudad y país en su perfil "
        "demográfico (HU005); sin esas secciones no coincide. Orden `user_id` ascendente con "
        "cursor keyset. Solo identificadores: `no-store`.",
        openapi_extra=_request_body(),
        responses={
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": f"`{PERMISSION_REQUIRED}`: la solicitud trae `Authorization` "
                "(un cliente no puede usar este contrato interno).",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `required`, `unknown_value` (también "
                "`other` o un país inexistente), `duplicate_value`, `too_many_items`, "
                "`invalid_length`, `out_of_range`, `field_not_allowed` o "
                f"`{INVALID_CURSOR}`.",
            },
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": f"`{ReadModelUnavailable.code}`: la base de lectura no responde.",
            },
        },
    )
    async def athletes_search(
        request: Request,
        authorization: Annotated[
            str | None,
            Header(description="No se envía: con ella la solicitud se rechaza (`403`)."),
        ] = None,
    ) -> Response:
        if authorization is not None:
            query.reject(AUTHORIZATION_PRESENT)
            return _no_store(
                error_response(
                    HTTPStatus.FORBIDDEN,
                    PERMISSION_REQUIRED,
                    "Esta operación no está disponible para los clientes.",
                )
            )
        # Solo se leen los bytes aquí; la consulta bloqueante corre en el pool.
        return _no_store(await run_in_threadpool(_search, query, await request.body()))

    return router
