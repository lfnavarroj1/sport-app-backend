"""HU011 (CA5): `GET /v1/me/authorized-athletes` (decisión del 2026-10-18).

Deportistas con autorización vigente hacia el profesional autenticado, de la vista
proyectada. Solo identificadores y fecha: `bff-web` agrega nombre y correo desde Usuarios.
"""

from datetime import datetime
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, Field

from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from training_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    forbidden,
)
from training_management.application.errors import PermissionRequired
from training_management.application.queries.authorized_athletes import (
    AuthorizedAthletesPage,
    GetAuthorizedAthletes,
)
from training_management.application.queries.training_history import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    InvalidHistoryQuery,
)


class AuthorizedAthleteItem(BaseModel):
    athlete_id: UUID
    authorized_at: datetime = Field(description="Instante (UTC) de la autorización vigente.")


class AuthorizedAthletesResponse(BaseModel):
    items: list[AuthorizedAthleteItem]
    next_cursor: str | None = Field(
        description="Cursor opaco de la página siguiente; `null` si no hay más."
    )

    @classmethod
    def from_page(cls, page: AuthorizedAthletesPage) -> "AuthorizedAthletesResponse":
        return cls(
            items=[
                AuthorizedAthleteItem(athlete_id=item.athlete_id, authorized_at=item.authorized_at)
                for item in page.items
            ],
            next_cursor=page.next_cursor,
        )


def build_authorized_athletes_router(
    *, verify_token: TokenVerifier, get_authorized_athletes: GetAuthorizedAthletes
) -> APIRouter:
    router = APIRouter(tags=["training-history"])

    @router.get(
        "/v1/me/authorized-athletes",
        response_model=AuthorizedAthletesResponse,
        summary="Deportistas que me autorizaron (HU011, CA5)",
        description="Solo el profesional con `training.history.view_assigned` y solo "
        "autorizaciones vigentes proyectadas; orden `authorized_at` y `athlete_id` "
        "descendentes. Una revocación lo retira cuando el proyector la aplica.",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: límite o cursor inválidos.",
            },
        },
    )
    def authorized_athletes(
        *,
        response: Response,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | AuthorizedAthletesResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            page = get_authorized_athletes(caller, limit=limit, cursor=cursor)
        except PermissionRequired as error:
            return forbidden(error)
        except InvalidHistoryQuery as error:
            return error_response(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                error.code,
                "La consulta no es válida.",
                [ErrorDetail(field=f"query.{error.field}", issue=error.issue)],
            )
        response.headers["Cache-Control"] = "no-store"
        return AuthorizedAthletesResponse.from_page(page)

    return router
