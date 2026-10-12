"""HU010: `POST /v1/users/lookup` (contrato interno aprobado el 2026-10-12).

Solo lo consume `bff-web` dentro de la red de la plataforma. El correo va en el
cuerpo y no en la ruta para que no quede en registros de acceso ni proxies.
"""

from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from users_management.application.queries.lookup_user import LookupUserByEmail
from users_management.domain.account.errors import InvalidRegistrationData
from users_management.domain.account.model import EMAIL_MAX_LENGTH, ActorType

USER_NOT_FOUND = "user_not_found"


class LookupUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Annotated[EmailStr, Field(max_length=EMAIL_MAX_LENGTH)]


class UserReferenceResponse(BaseModel):
    user_id: UUID
    actor_type: ActorType


def build_lookup_router(lookup_user: LookupUserByEmail) -> APIRouter:
    router = APIRouter(prefix="/v1/users", tags=["users"])

    @router.post(
        "/lookup",
        response_model=UserReferenceResponse,
        summary="Resolver un correo exacto a usuario y tipo de actor (interno, HU010)",
        responses={
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": f"`{USER_NOT_FOUND}`: no hay una cuenta activa con ese correo.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`.",
            },
        },
    )
    def lookup(body: LookupUserRequest) -> Response | UserReferenceResponse:
        try:
            reference = lookup_user(str(body.email))
        except InvalidRegistrationData as error:
            return error_response(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                error.code,
                "La solicitud no es válida.",
                [ErrorDetail(field="body.email", issue=error.issue)],
            )
        if reference is None:
            return error_response(
                HTTPStatus.NOT_FOUND, USER_NOT_FOUND, "No existe una cuenta activa con ese correo."
            )
        return UserReferenceResponse(user_id=reference.user_id, actor_type=reference.actor_type)

    return router
