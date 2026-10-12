"""HU001: `POST /v1/users` (contrato aprobado el 2026-10-07)."""

from datetime import datetime
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, StringConstraints

from sportapp_platform.correlation import current_correlation_id
from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from users_management.application.commands.register_user import (
    RegisterUser,
    RegisterUserCommand,
    RegistrationResult,
)
from users_management.application.errors import IdempotencyKeyReused
from users_management.domain.account.errors import (
    EmailAlreadyRegistered,
    InvalidRegistrationData,
    PoliciesVersionMismatch,
)
from users_management.domain.account.model import (
    EMAIL_MAX_LENGTH,
    FULL_NAME_MAX_LENGTH,
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    AccountStatus,
    ActorType,
)

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
INVALID_REQUEST_MESSAGE = "La solicitud no es válida."


class RegisterUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=FULL_NAME_MAX_LENGTH)
    ]
    email: Annotated[EmailStr, Field(max_length=EMAIL_MAX_LENGTH)]
    password: Annotated[
        SecretStr, Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)
    ]
    actor_type: ActorType
    accepted_policies_version: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)
    ]


class UserRegisteredResponse(BaseModel):
    user_id: UUID
    full_name: str
    email: str
    actor_type: ActorType
    status: AccountStatus
    aggregate_version: int
    created_at: datetime

    @classmethod
    def from_result(cls, result: RegistrationResult) -> "UserRegisteredResponse":
        return cls(
            user_id=result.user_id,
            full_name=result.full_name,
            email=result.email,
            actor_type=result.actor_type,
            status=AccountStatus(result.status),
            aggregate_version=result.aggregate_version,
            created_at=result.created_at,
        )


def _correlation_uuid() -> UUID | None:
    correlation_id = current_correlation_id()
    return UUID(correlation_id) if correlation_id else None


def build_registration_router(register_user: RegisterUser) -> APIRouter:
    router = APIRouter(prefix="/v1/users", tags=["users"])

    @router.post(
        "",
        status_code=HTTPStatus.CREATED,
        response_model=UserRegisteredResponse,
        summary="Registrar usuario (HU001)",
        responses={
            HTTPStatus.CREATED.value: {
                "headers": {
                    "Location": {"schema": {"type": "string"}},
                    REPLAYED_HEADER: {
                        "description": "`true` cuando se devuelve el resultado de un reintento.",
                        "schema": {"type": "string"},
                    },
                }
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`email_already_registered` o `idempotency_key_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` o `policies_version_mismatch`.",
            },
        },
    )
    def register(
        body: RegisterUserRequest,
        response: Response,
        idempotency_key: Annotated[UUID, Header(alias=IDEMPOTENCY_HEADER)],
    ) -> Response | UserRegisteredResponse:
        command = RegisterUserCommand(
            full_name=body.full_name,
            email=str(body.email),
            password=body.password.get_secret_value(),
            actor_type=body.actor_type,
            accepted_policies_version=body.accepted_policies_version,
            idempotency_key=idempotency_key,
            correlation_id=_correlation_uuid(),
        )
        try:
            result = register_user(command)
        except InvalidRegistrationData as error:
            return error_response(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                error.code,
                INVALID_REQUEST_MESSAGE,
                [ErrorDetail(field=f"body.{error.field}", issue=error.issue)],
            )
        except PoliciesVersionMismatch as error:
            return error_response(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                error.code,
                "Debe aceptar la versión vigente de las políticas.",
                [ErrorDetail(field="body.accepted_policies_version", issue=error.code)],
            )
        except EmailAlreadyRegistered as error:
            return error_response(HTTPStatus.CONFLICT, error.code, "El correo ya está registrado.")
        except IdempotencyKeyReused as error:
            return error_response(
                HTTPStatus.CONFLICT,
                error.code,
                "La clave de idempotencia ya se usó con otra solicitud.",
            )

        response.headers["Location"] = f"/v1/users/{result.user_id}"
        if result.replayed:
            response.headers[REPLAYED_HEADER] = "true"
        return UserRegisteredResponse.from_result(result)

    return router
