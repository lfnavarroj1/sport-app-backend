"""Contratos internos de identidad que consume solo `bff-web` dentro de la red de la
plataforma.

- HU010: `POST /v1/users/lookup` (aprobado el 2026-10-12). El correo va en el cuerpo y no en
  la ruta para que no quede en registros de acceso ni proxies.
- HU011 (CA5, aprobado el 2026-10-18): `POST /v1/users/lookup-batch`, nombre y correo de las
  cuentas activas de un lote de identificadores. Datos personales: no se registran.
- HU022 (ADR-039): `POST /v1/users/directory-lookup`, solo el nombre (`full_name`) de las
  cuentas activas de un lote, para los inscritos del organizador; sin correo ni perfil.
"""

from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from users_management.application.queries.directory_lookup import LookupDirectoryNames
from users_management.application.queries.lookup_user import LookupUserByEmail
from users_management.application.queries.lookup_users_batch import (
    MAX_BATCH,
    InvalidLookupBatch,
    LookupUsersByIds,
)
from users_management.domain.account.errors import InvalidRegistrationData
from users_management.domain.account.model import EMAIL_MAX_LENGTH, ActorType

USER_NOT_FOUND = "user_not_found"


class LookupUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Annotated[EmailStr, Field(max_length=EMAIL_MAX_LENGTH)]


class UserReferenceResponse(BaseModel):
    user_id: UUID
    actor_type: ActorType


class LookupUsersBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_ids: Annotated[
        list[UUID],
        Field(
            min_length=1,
            max_length=MAX_BATCH,
            description="Entre 1 y 100 identificadores únicos.",
        ),
    ]


class UserDisplayItem(BaseModel):
    user_id: UUID
    full_name: str
    email: str


class LookupUsersBatchResponse(BaseModel):
    items: list[UserDisplayItem] = Field(
        description="Cuentas activas encontradas, en el orden de la solicitud. Una cuenta "
        "inexistente, deshabilitada o aún sin nombre proyectado se omite."
    )


class DirectoryLookupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_ids: Annotated[
        list[UUID],
        Field(
            min_length=1,
            max_length=MAX_BATCH,
            description="Entre 1 y 100 identificadores únicos.",
        ),
    ]


class DirectoryNameItem(BaseModel):
    user_id: UUID
    full_name: str


class DirectoryLookupResponse(BaseModel):
    items: list[DirectoryNameItem] = Field(
        description="Cuentas activas encontradas, en el orden de la solicitud, solo con "
        "`full_name`. Una cuenta inexistente, deshabilitada o aún sin nombre proyectado se omite."
    )


def build_lookup_router(
    lookup_user: LookupUserByEmail,
    lookup_batch: LookupUsersByIds,
    directory_lookup: LookupDirectoryNames,
) -> APIRouter:
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

    @router.post(
        "/lookup-batch",
        response_model=LookupUsersBatchResponse,
        summary="Nombre y correo de un lote de cuentas activas (interno, HU011)",
        responses={
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: lote vacío, de más de 100 o con "
                "identificadores repetidos (`duplicate_value`).",
            },
        },
    )
    def lookup_users_batch(
        body: LookupUsersBatchRequest, response: Response
    ) -> Response | LookupUsersBatchResponse:
        try:
            found = lookup_batch(body.user_ids)
        except InvalidLookupBatch as error:
            return error_response(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                error.code,
                "La solicitud no es válida.",
                [ErrorDetail(field="body.user_ids", issue=error.issue)],
            )
        response.headers["Cache-Control"] = "no-store"
        return LookupUsersBatchResponse(
            items=[
                UserDisplayItem(user_id=item.user_id, full_name=item.full_name, email=item.email)
                for item in found
            ]
        )

    @router.post(
        "/directory-lookup",
        response_model=DirectoryLookupResponse,
        summary="Nombre de un lote de cuentas activas, sin correo (interno, HU022)",
        description="Solo para `bff-web` (inscritos del organizador, ADR-039): identidad mínima, "
        "sin correo, perfil ni datos de salud. Cada consulta se audita sin nombres.",
        responses={
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: lote vacío (`too_short`), de más de 100 "
                "(`too_long`), con identificadores repetidos (`duplicate_value`) o que no son "
                "UUID (`uuid_parsing`).",
            },
        },
    )
    def directory_lookup_names(
        body: DirectoryLookupRequest, response: Response
    ) -> Response | DirectoryLookupResponse:
        try:
            found = directory_lookup(body.user_ids)
        except InvalidLookupBatch as error:
            return error_response(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                error.code,
                "La solicitud no es válida.",
                [ErrorDetail(field="body.user_ids", issue=error.issue)],
            )
        response.headers["Cache-Control"] = "no-store"
        return DirectoryLookupResponse(
            items=[
                DirectoryNameItem(user_id=item.user_id, full_name=item.full_name) for item in found
            ]
        )

    return router
