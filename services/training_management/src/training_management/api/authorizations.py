"""HU010: autorización del deportista a un profesional (contrato aprobado el 2026-10-12).

`PUT /v1/training-authorizations/{professional_id}`: el deportista autenticado
autoriza al profesional (`201` si la crea o reactiva, `200` si ya estaba activa).
`DELETE` la revoca (`204`, también si no estaba activa). El deportista siempre es
quien llama: su identificador sale del token.
"""

from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Response
from pydantic import BaseModel

from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from training_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    correlation_uuid,
    forbidden,
    unavailable,
)
from training_management.application.commands.authorizations import (
    GrantAuthorization,
    RevokeAuthorization,
)
from training_management.application.errors import (
    CapabilitiesUnavailable,
    CapabilityRequired,
    PermissionRequired,
)
from training_management.domain.authorizations.model import SelfAuthorization, TrainingAuthorization


class TrainingAuthorizationResponse(BaseModel):
    athlete_id: UUID
    professional_id: UUID
    status: Literal["ACTIVE"]
    granted_at: datetime

    @classmethod
    def from_authorization(
        cls, authorization: TrainingAuthorization
    ) -> "TrainingAuthorizationResponse":
        return cls(
            athlete_id=authorization.athlete_id,
            professional_id=authorization.professional_id,
            status="ACTIVE",
            granted_at=authorization.granted_at,
        )


def build_authorizations_router(
    *,
    verify_token: TokenVerifier,
    grant: GrantAuthorization,
    revoke: RevokeAuthorization,
) -> APIRouter:
    router = APIRouter(prefix="/v1/training-authorizations", tags=["training-authorizations"])

    @router.put(
        "/{professional_id}",
        response_model=TrainingAuthorizationResponse,
        summary="Autorizar a un profesional para asignar planes (HU010)",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.CREATED.value: {
                "model": TrainingAuthorizationResponse,
                "description": "Autorización creada o reactivada.",
            },
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` (no es deportista) o "
                "`capability_required` (`training.plan.manage`).",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`.",
            },
        },
    )
    def grant_authorization(
        professional_id: UUID,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | TrainingAuthorizationResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            result = grant(caller, professional_id, correlation_uuid())
        except (PermissionRequired, CapabilityRequired) as error:
            return forbidden(error)
        except CapabilitiesUnavailable:
            return unavailable()
        except SelfAuthorization as error:
            return error_response(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                error.code,
                "La solicitud no es válida.",
                [ErrorDetail(field="path.professional_id", issue="self_authorization")],
            )
        response.status_code = HTTPStatus.CREATED if result.created else HTTPStatus.OK
        return TrainingAuthorizationResponse.from_authorization(result.authorization)

    @router.delete(
        "/{professional_id}",
        status_code=HTTPStatus.NO_CONTENT,
        summary="Revocar la autorización de un profesional (HU010)",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required`: solo el deportista revoca.",
            },
        },
    )
    def revoke_authorization(
        professional_id: UUID,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            revoke(caller, professional_id, correlation_uuid())
        except PermissionRequired as error:
            return forbidden(error)
        return Response(status_code=HTTPStatus.NO_CONTENT)

    return router
