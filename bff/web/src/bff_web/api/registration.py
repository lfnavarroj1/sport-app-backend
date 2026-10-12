"""HU001 en el canal web: `POST /v1/users`.

El BFF valida el formato de transporte y reenvía al servicio propietario. Las
reglas autoritativas (unicidad, hash, políticas vigentes, idempotencia) viven en
`users_management`; sus errores se entregan sin alterar.
"""

import logging
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, StringConstraints

from bff_web.clients.users_management import (
    UpstreamTimeout,
    UpstreamUnavailable,
    UsersManagementClient,
)
from sportapp_platform.errors import ErrorResponse, error_response

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
_PASSTHROUGH_STATUSES = {
    HTTPStatus.CREATED,
    HTTPStatus.CONFLICT,
    HTTPStatus.UNPROCESSABLE_ENTITY,
}

_logger = logging.getLogger("sportapp.bff")

ActorType = Literal["athlete", "organizer", "service_provider"]


class RegisterUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
    ]
    email: Annotated[EmailStr, Field(max_length=254)]
    password: Annotated[SecretStr, Field(min_length=12, max_length=128)]
    actor_type: ActorType
    accepted_policies_version: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)
    ]


class UserRegisteredResponse(BaseModel):
    user_id: UUID
    full_name: str
    email: str
    actor_type: ActorType
    status: Literal["active"]
    aggregate_version: int
    created_at: datetime


def build_registration_router(users: UsersManagementClient) -> APIRouter:
    router = APIRouter(prefix="/v1/users", tags=["users"])

    @router.post(
        "",
        status_code=HTTPStatus.CREATED,
        response_model=UserRegisteredResponse,
        summary="Registrar usuario (HU001)",
        responses={
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`email_already_registered` o `idempotency_key_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` o `policies_version_mismatch`.",
            },
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": "`upstream_unavailable`.",
            },
            HTTPStatus.GATEWAY_TIMEOUT.value: {
                "model": ErrorResponse,
                "description": "`upstream_timeout`.",
            },
        },
    )
    def register(
        body: RegisterUserRequest,
        idempotency_key: Annotated[UUID, Header(alias=IDEMPOTENCY_HEADER)],
    ) -> Response:
        payload = body.model_dump(mode="json") | {"password": body.password.get_secret_value()}
        try:
            upstream = users.register_user(payload, idempotency_key)
        except UpstreamTimeout as error:
            return error_response(
                HTTPStatus.GATEWAY_TIMEOUT, error.code, "El servicio de usuarios no respondió."
            )
        except UpstreamUnavailable as error:
            return error_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                error.code,
                "El servicio de usuarios no está disponible.",
            )

        if upstream.status_code not in _PASSTHROUGH_STATUSES:
            _logger.warning(
                "bff.upstream_unexpected_status",
                extra={"fields": {"upstream": "users-management", "status": upstream.status_code}},
            )
            return error_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                UpstreamUnavailable.code,
                "El servicio de usuarios no está disponible.",
            )

        headers = {
            name: upstream.headers[name]
            for name in ("Location", REPLAYED_HEADER)
            if name in upstream.headers
        }
        return JSONResponse(
            status_code=upstream.status_code, content=upstream.json(), headers=headers
        )

    return router
