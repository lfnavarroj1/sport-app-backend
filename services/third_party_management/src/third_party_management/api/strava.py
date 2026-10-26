"""HU031 (ADR-042): integración del deportista con Strava.

- `POST /v1/me/integrations/strava/authorization`: URL del consentimiento oficial (`201`).
- `POST /v1/integrations/strava/callback`: interno, sin token, solo lo llama `bff-mobile`;
  responde siempre `200` con el resultado (`422` solo por la forma del cuerpo).
- `GET /v1/me/integrations/strava`: estado de la vinculación y última corrida.
- `DELETE /v1/me/integrations/strava`: desvincula (`204`, idempotente).
- `POST /v1/me/integrations/strava/syncs`: sincronización manual (`202`, `Idempotency-Key`).

Las operaciones autenticadas exigen `athlete` con `integrations.strava.connect` también en
el servicio (ADR-024). Todas las respuestas llevan `Cache-Control: no-store`. Ningún
token, `code` ni `state` sale del servicio ni se registra.
"""

from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Response
from pydantic import BaseModel, ConfigDict, Field

from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from third_party_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    correlation_uuid,
    forbidden,
)
from third_party_management.application.access import STRAVA_CAPABILITY, require_athlete
from third_party_management.application.connections import GetIntegrationStatus, UnlinkStrava
from third_party_management.application.errors import (
    CapabilitiesTimeout,
    CapabilitiesUnavailable,
    CapabilityRequired,
    IntegrationNotLinked,
    PermissionRequired,
    StravaNotConfigured,
    SyncInProgress,
)
from third_party_management.application.oauth import (
    CallbackInput,
    CompleteCallback,
    StartAuthorization,
)
from third_party_management.application.syncs import RequestSync
from third_party_management.domain.connections import NOT_LINKED
from third_party_management.domain.synchronization import SyncRun

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
CALLBACK_FIELD_MAX = 512

IntegrationState = Literal["NOT_LINKED", "ACTIVE", "REVOKED", "EXPIRED"]
SyncState = Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "RATE_LIMITED"]
SyncError = Literal[
    "rate_limited",
    "provider_unavailable",
    "circuit_open",
    "token_expired",
    "insufficient_scope",
    "provider_rejected",
    "invalid_response",
    "invalid_activity",
    "integration_revoked",
]
CallbackField = Annotated[str | None, Field(default=None, max_length=CALLBACK_FIELD_MAX)]


class AuthorizationResponse(BaseModel):
    authorization_url: str = Field(
        description="Consentimiento oficial de Strava con `scope=read,activity:read` y un "
        "`state` de un solo uso; el móvil la abre en Custom Tabs."
    )
    expires_at: datetime = Field(description="Vencimiento del `state` (10 minutos), en UTC.")


class CallbackRequest(BaseModel):
    """Parámetros que Strava devolvió al callback público de `bff-mobile`."""

    model_config = ConfigDict(extra="forbid")

    code: CallbackField = None
    state: CallbackField = None
    scope: CallbackField = None
    error: CallbackField = None


class CallbackResponse(BaseModel):
    result: Literal["LINKED", "DENIED", "INVALID_STATE", "ERROR"]


class LastSyncResponse(BaseModel):
    sync_run_id: UUID
    status: SyncState
    requested_at: datetime
    finished_at: datetime | None
    imported_count: int = Field(description="Actividades nuevas o cambiadas publicadas.")
    error_code: SyncError | None

    @classmethod
    def from_run(cls, run: SyncRun) -> "LastSyncResponse":
        return cls(
            sync_run_id=run.sync_run_id,
            status=run.status.value,
            requested_at=run.requested_at,
            finished_at=run.finished_at,
            imported_count=run.imported_count,
            error_code=run.error_code.value if run.error_code else None,
        )


class IntegrationStatusResponse(BaseModel):
    status: IntegrationState
    external_athlete_id: str | None = Field(description="Identificador del atleta en Strava.")
    linked_at: datetime | None
    scopes: list[str] = Field(description="Scopes aceptados; vacío sin vinculación.")
    last_sync: LastSyncResponse | None


class SyncAcceptedResponse(BaseModel):
    sync_run_id: UUID
    status: SyncState = Field(
        description="`QUEUED` al aceptarla; un reintento con la misma "
        "clave devuelve el estado actual de la corrida."
    )


_MESSAGES: dict[type[Exception], tuple[HTTPStatus, str]] = {
    IntegrationNotLinked: (HTTPStatus.CONFLICT, "No hay una cuenta de Strava vinculada."),
    SyncInProgress: (HTTPStatus.CONFLICT, "Ya hay una sincronización en curso."),
    CapabilitiesTimeout: (
        HTTPStatus.GATEWAY_TIMEOUT,
        "No fue posible verificar su plan a tiempo. Intente de nuevo.",
    ),
    CapabilitiesUnavailable: (
        HTTPStatus.SERVICE_UNAVAILABLE,
        "No fue posible verificar su plan. Intente de nuevo.",
    ),
    StravaNotConfigured: (
        HTTPStatus.SERVICE_UNAVAILABLE,
        "La integración con Strava no está disponible. Intente más tarde.",
    ),
}
_HANDLED: tuple[type[Exception], ...] = (PermissionRequired, CapabilityRequired, *_MESSAGES)


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def _failure(error: Exception) -> Response:
    if isinstance(error, PermissionRequired | CapabilityRequired):
        return _no_store(forbidden(error))
    status, message = _MESSAGES[type(error)]
    return _no_store(error_response(status, getattr(error, "code", ""), message))


def _idempotency_key(raw: str | None) -> UUID | Response:
    if raw is None or not raw.strip():
        issue = "required"
    else:
        try:
            return UUID(raw.strip())
        except ValueError:
            issue = "invalid_uuid"
    return _no_store(
        error_response(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            "La solicitud no es válida.",
            [ErrorDetail(field=IDEMPOTENCY_HEADER, issue=issue)],
        )
    )


_FORBIDDEN = {
    "model": ErrorResponse,
    "description": "`permission_required` (no es deportista) o `capability_required` "
    f"(`{STRAVA_CAPABILITY}`).",
}
_UPSTREAM: dict[int | str, dict[str, Any]] = {
    HTTPStatus.SERVICE_UNAVAILABLE.value: {
        "model": ErrorResponse,
        "description": "`upstream_unavailable`: no se pudo verificar la identidad o el plan, "
        "o la integración con Strava no está configurada.",
    },
    HTTPStatus.GATEWAY_TIMEOUT.value: {
        "model": ErrorResponse,
        "description": "`upstream_timeout`: Afiliación no respondió a tiempo.",
    },
}
_PROTECTED: dict[int | str, dict[str, Any]] = {
    **AUTH_RESPONSES,
    HTTPStatus.FORBIDDEN.value: _FORBIDDEN,
    **_UPSTREAM,
}
_NO_STORE_HEADER = {"Cache-Control": {"description": "`no-store`.", "schema": {"type": "string"}}}


def build_strava_router(
    *,
    verify_token: TokenVerifier,
    start_authorization: StartAuthorization,
    complete_callback: CompleteCallback,
    get_status: GetIntegrationStatus,
    unlink: UnlinkStrava,
    request_sync: RequestSync,
) -> APIRouter:
    router = APIRouter(tags=["strava"])

    @router.post(
        "/v1/me/integrations/strava/authorization",
        status_code=HTTPStatus.CREATED,
        response_model=AuthorizationResponse,
        summary="Iniciar la vinculación con Strava (HU031)",
        description="Crea un `state` de un solo uso (10 minutos) ligado al deportista y "
        "devuelve la URL del consentimiento oficial. El móvil nunca captura credenciales de "
        "Strava.",
        responses={HTTPStatus.CREATED.value: {"headers": _NO_STORE_HEADER}, **_PROTECTED},
    )
    def start(
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | AuthorizationResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return _no_store(caller)
        try:
            link = start_authorization(caller)
        except _HANDLED as error:
            return _failure(error)
        _no_store(response)
        return AuthorizationResponse(
            authorization_url=link.authorization_url, expires_at=link.expires_at
        )

    @router.post(
        "/v1/integrations/strava/callback",
        response_model=CallbackResponse,
        summary="Completar el callback OAuth de Strava (interno, HU031)",
        description="Solo lo llama `bff-mobile`, sin token de SportApp. `INVALID_STATE` si el "
        "`state` falta, no existe, venció, ya se usó o no corresponde; `DENIED` si el usuario "
        "rechazó o no concedió `activity:read`; `ERROR` si el intercambio del `code` falló o "
        "la cuenta de Strava ya está vinculada a otro deportista; `LINKED` si quedó vinculada.",
        responses={
            HTTPStatus.OK.value: {"headers": _NO_STORE_HEADER},
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": "`upstream_unavailable`: Strava no está configurado.",
            },
        },
    )
    def callback(response: Response, body: CallbackRequest) -> Response | CallbackResponse:
        try:
            result = complete_callback(
                CallbackInput(code=body.code, state=body.state, scope=body.scope, error=body.error)
            )
        except StravaNotConfigured as error:
            return _failure(error)
        _no_store(response)
        return CallbackResponse(result=result.value)

    @router.get(
        "/v1/me/integrations/strava",
        response_model=IntegrationStatusResponse,
        summary="Estado de la vinculación con Strava (HU031)",
        responses={HTTPStatus.OK.value: {"headers": _NO_STORE_HEADER}, **_PROTECTED},
    )
    def status(
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | IntegrationStatusResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return _no_store(caller)
        try:
            current = get_status(caller)
        except _HANDLED as error:
            return _failure(error)
        _no_store(response)
        account, run = current.account, current.last_sync
        last_sync = LastSyncResponse.from_run(run) if run is not None else None
        if account is None:
            return IntegrationStatusResponse(
                status=NOT_LINKED,
                external_athlete_id=None,
                linked_at=None,
                scopes=[],
                last_sync=last_sync,
            )
        return IntegrationStatusResponse(
            status=account.status.value,
            external_athlete_id=account.external_athlete_id,
            linked_at=account.linked_at,
            scopes=list(account.scopes),
            last_sync=last_sync,
        )

    @router.delete(
        "/v1/me/integrations/strava",
        status_code=HTTPStatus.NO_CONTENT,
        response_class=Response,
        summary="Desvincular Strava (HU031)",
        description="Revoca en Strava (mejor esfuerzo), borra los tokens, marca `REVOKED` y "
        "cancela las corridas pendientes; las actividades importadas se conservan. Siempre "
        "`204`, también sin vinculación.",
        responses={HTTPStatus.NO_CONTENT.value: {"headers": _NO_STORE_HEADER}, **_PROTECTED},
    )
    def delete(authorization: Annotated[str | None, Header()] = None) -> Response:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return _no_store(caller)
        try:
            unlink(caller)
        except _HANDLED as error:
            return _failure(error)
        return _no_store(Response(status_code=HTTPStatus.NO_CONTENT))

    @router.post(
        "/v1/me/integrations/strava/syncs",
        status_code=HTTPStatus.ACCEPTED,
        response_model=SyncAcceptedResponse,
        summary="Solicitar una sincronización manual con Strava (HU031)",
        description="La importación es asíncrona (`terceros-sync-worker`). Reintentar con la "
        "misma `Idempotency-Key` (24 horas) devuelve la misma corrida con su estado actual y "
        "`Idempotent-Replayed: true`.",
        responses={
            HTTPStatus.ACCEPTED.value: {
                "headers": {
                    **_NO_STORE_HEADER,
                    REPLAYED_HEADER: {
                        "description": "`true` cuando se devuelve el resultado de un reintento.",
                        "schema": {"type": "string"},
                    },
                }
            },
            **_PROTECTED,
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`integration_not_linked` (sin vinculación `ACTIVE`) o "
                "`sync_in_progress` (ya hay una corrida `QUEUED`/`RUNNING`).",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `Idempotency-Key` `required` o `invalid_uuid`.",
            },
        },
    )
    def sync(
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | SyncAcceptedResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return _no_store(caller)
        try:
            # El actor se evalúa antes que la forma de la clave (mismo orden que el BFF).
            require_athlete(caller)
        except PermissionRequired as error:
            return _failure(error)
        key = _idempotency_key(idempotency_key)
        if isinstance(key, Response):
            return key
        try:
            result = request_sync(caller, key, correlation_uuid())
        except _HANDLED as error:
            return _failure(error)
        _no_store(response)
        if result.replayed:
            response.headers[REPLAYED_HEADER] = "true"
        return SyncAcceptedResponse(
            sync_run_id=result.run.sync_run_id, status=result.run.status.value
        )

    return router
