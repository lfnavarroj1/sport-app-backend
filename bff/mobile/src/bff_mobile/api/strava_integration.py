"""HU031 en el canal móvil: vinculación y sincronización con Strava (ADR-042).

Operaciones del deportista (`/v1/me/integrations/strava...`): el BFF autentica (JWKS), exige
`athlete` y la capacidad vigente `integrations.strava.connect` en Afiliación antes de llamar a
`third_party_management`, que vuelve a verificar todo. Después reenvía el token,
`Idempotency-Key` (solo en la sincronización; si falta la rechaza Terceros) y la correlación
sin reinterpretarlos. Respuestas, códigos (`sync_in_progress`, `integration_not_linked`) e
`Idempotent-Replayed` llegan sin alterar; la autorización y el estado salen con
`Cache-Control: no-store` (la URL de autorización lleva el `state`).

Callback público (`GET /v1/integrations/strava/callback`): es el `redirect_uri` registrado en
Strava y no lleva token de SportApp. Reenvía `code`, `state`, `scope` y `error` en el cuerpo
JSON del contrato interno de Terceros y redirige (`302`) al enlace profundo
`sportapp://integrations/strava?result=<RESULT>`. Si Terceros no responde, tarda, responde un
estado distinto de `200` (también `422`) o un `result` fuera del catálogo, el resultado es
`ERROR`: el navegador nunca recibe un error JSON ni detalles del proveedor (CA3).

Ningún `code`, `state`, `scope`, token ni URL completa del callback se escribe en los logs: el
registro `strava.callback` lleva solo el `result`. Ningún token de Strava llega a este BFF.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal, get_args
from uuid import UUID

import httpx
from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from bff_mobile.clients.third_party_management import ThirdPartyManagementClient
from bff_mobile.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.errors import ErrorResponse, error_response
from sportapp_platform.tokens import InvalidAccessToken, JwksUnavailable, VerifiedAccessToken

ATHLETE = "athlete"
BEARER_PREFIX = "bearer "
STRAVA_CAPABILITY = "integrations.strava.connect"
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
UPSTREAM = "third-party-management"
DEEP_LINK = "sportapp://integrations/strava"
# Longitud máxima que acepta Terceros en cada parámetro del callback (ADR-042); el BFF solo
# la documenta: un valor más largo lo rechaza Terceros (`422`) y el resultado es `ERROR`.
CALLBACK_PARAMETER_MAX_LENGTH = 512
INSTANT = "Instante UTC con sufijo `Z`."
# Cabeceras del servicio que el cliente necesita tal como llegan.
RELAYED_HEADERS = ("Cache-Control", "Location", REPLAYED_HEADER, "WWW-Authenticate")

_logger = logging.getLogger("sportapp.bff")

IntegrationStatus = Literal["NOT_LINKED", "ACTIVE", "REVOKED", "EXPIRED"]
SyncStatus = Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "RATE_LIMITED"]
StravaLinkResult = Literal["LINKED", "DENIED", "INVALID_STATE", "ERROR"]
CALLBACK_RESULTS: tuple[str, ...] = get_args(StravaLinkResult)
LINK_ERROR = "ERROR"
# Catálogo `error_code` de una corrida de `third-party-management` (ADR-042, R), en su orden;
# el BFF solo lo documenta.
SyncErrorCode = Literal[
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


class StravaAuthorizationResponse(BaseModel):
    """URL que el móvil abre en Custom Tabs; el móvil nunca captura credenciales de Strava."""

    authorization_url: Annotated[
        str,
        Field(
            description="Autorización de Strava con `client_id`, `redirect_uri`, "
            "`response_type=code`, `approval_prompt=auto`, `scope=read,activity:read` y "
            "`state` de un solo uso."
        ),
    ]
    expires_at: Annotated[
        datetime, Field(description=f"{INSTANT} Vencimiento del `state` (10 minutos).")
    ]


class StravaSyncSummary(BaseModel):
    """Última corrida de sincronización del deportista."""

    sync_run_id: UUID
    status: SyncStatus
    requested_at: Annotated[datetime, Field(description=INSTANT)]
    finished_at: Annotated[
        datetime | None, Field(description=f"{INSTANT} `null` mientras no termina.")
    ]
    imported_count: Annotated[
        int, Field(description="Actividades publicadas por la corrida (nuevas o con cambios).")
    ]
    error_code: Annotated[SyncErrorCode | None, Field(description="`null` si la corrida no falló.")]


class StravaIntegrationResponse(BaseModel):
    """Estado de la vinculación con Strava; nunca contiene tokens."""

    status: IntegrationStatus
    external_athlete_id: Annotated[
        str | None, Field(description="Identificador del atleta en Strava, como texto.")
    ]
    linked_at: Annotated[datetime | None, Field(description=INSTANT)]
    scopes: Annotated[list[str], Field(description="Scopes aceptados; vacío si `NOT_LINKED`.")]
    last_sync: Annotated[
        StravaSyncSummary | None, Field(description="`null` si nunca se sincronizó.")
    ]


class StravaSyncResponse(BaseModel):
    """Corrida aceptada (`QUEUED`); con la misma clave, la corrida original y su estado actual."""

    sync_run_id: UUID
    status: SyncStatus


def _unauthenticated() -> Response:
    response = error_response(
        HTTPStatus.UNAUTHORIZED, InvalidAccessToken.code, "La sesión no es válida."
    )
    response.headers["WWW-Authenticate"] = "Bearer"
    return response


def _failure(error: UpstreamTimeout | UpstreamUnavailable) -> Response:
    if isinstance(error, UpstreamTimeout):
        return error_response(
            HTTPStatus.GATEWAY_TIMEOUT, error.code, "Un servicio interno no respondió."
        )
    return error_response(
        HTTPStatus.SERVICE_UNAVAILABLE, error.code, "Un servicio interno no está disponible."
    )


def _log_unexpected(status: int) -> None:
    _logger.warning(
        "bff.upstream_unexpected_status",
        extra={"fields": {"upstream": UPSTREAM, "status": status}},
    )


def _relayed_headers(upstream: httpx.Response) -> dict[str, str]:
    return {name: upstream.headers[name] for name in RELAYED_HEADERS if name in upstream.headers}


def _passthrough(upstream: httpx.Response, allowed: set[int]) -> Response:
    if upstream.status_code not in allowed:
        _log_unexpected(upstream.status_code)
        return _failure(UpstreamUnavailable())
    headers = _relayed_headers(upstream)
    if upstream.status_code == HTTPStatus.NO_CONTENT:
        return Response(status_code=HTTPStatus.NO_CONTENT, headers=headers)
    try:
        content = upstream.json()
    except ValueError:
        _log_unexpected(upstream.status_code)
        return _failure(UpstreamUnavailable())
    return JSONResponse(status_code=upstream.status_code, content=content, headers=headers)


def _link_result(upstream: httpx.Response) -> str:
    """`result` del contrato interno; cualquier otra respuesta de Terceros es `ERROR`."""
    if upstream.status_code != HTTPStatus.OK:
        _log_unexpected(upstream.status_code)
        return LINK_ERROR
    try:
        document = upstream.json()
    except ValueError:
        _log_unexpected(upstream.status_code)
        return LINK_ERROR
    result = document.get("result") if isinstance(document, dict) else None
    if result in CALLBACK_RESULTS:
        return str(result)
    _log_unexpected(upstream.status_code)
    return LINK_ERROR


def _edge() -> dict[int | str, dict[str, Any]]:
    return {
        HTTPStatus.UNAUTHORIZED.value: {
            "model": ErrorResponse,
            "description": "`invalid_access_token`: falta el token o no es válido.",
        },
        HTTPStatus.FORBIDDEN.value: {
            "model": ErrorResponse,
            "description": "`permission_required` (no es deportista) o `capability_required` "
            f"(`{STRAVA_CAPABILITY}`).",
        },
        HTTPStatus.SERVICE_UNAVAILABLE.value: {
            "model": ErrorResponse,
            "description": "`upstream_unavailable`: capacidad no verificable, servicio caído "
            "o integración con Strava sin configurar.",
        },
        HTTPStatus.GATEWAY_TIMEOUT.value: {
            "model": ErrorResponse,
            "description": "`upstream_timeout`: capacidad o servicio sin respuesta.",
        },
    }


_NO_STORE = {"Cache-Control": {"description": "`no-store`.", "schema": {"type": "string"}}}
_REPLAY = {
    REPLAYED_HEADER: {
        "description": "`true` cuando se devuelve la corrida original de la misma clave.",
        "schema": {"type": "string"},
    }
}
_REDIRECT = {
    "Location": {
        "description": f"`{DEEP_LINK}?result=<RESULT>` con `RESULT` en "
        + ", ".join(f"`{result}`" for result in CALLBACK_RESULTS)
        + ".",
        "schema": {"type": "string"},
    },
    **_NO_STORE,
}
_EDGE_STATUSES = {
    HTTPStatus.UNAUTHORIZED,
    HTTPStatus.FORBIDDEN,
    HTTPStatus.SERVICE_UNAVAILABLE,
    HTTPStatus.GATEWAY_TIMEOUT,
}


def _callback_parameter(description: str) -> Any:
    """Parámetro opcional del callback; su longitud la valida Terceros, no el BFF."""
    return Query(
        description=f"{description} Máximo {CALLBACK_PARAMETER_MAX_LENGTH} caracteres "
        "(lo valida Terceros; más largo → `ERROR`).",
        json_schema_extra={"maxLength": CALLBACK_PARAMETER_MAX_LENGTH},
    )


def build_strava_integration_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    athlete_snapshot: Callable[[UUID], dict[str, Any]],
    third_party: ThirdPartyManagementClient,
) -> APIRouter:
    router = APIRouter(tags=["integrations"])

    def authenticate_athlete(authorization: str | None) -> tuple[UUID, str] | Response:
        if not authorization or not authorization.lower().startswith(BEARER_PREFIX):
            return _unauthenticated()
        token = authorization[len(BEARER_PREFIX) :].strip()
        try:
            identity = verify_token(token)
            user_id = UUID(identity.subject)
        except (InvalidAccessToken, ValueError):
            return _unauthenticated()
        except JwksUnavailable:
            _logger.warning("bff.jwks_unavailable")
            return _failure(UpstreamUnavailable())
        if identity.actor_type != ATHLETE:
            return error_response(
                HTTPStatus.FORBIDDEN,
                "permission_required",
                "Su tipo de usuario no puede realizar esta operación.",
            )
        return user_id, token

    def authorized_token(authorization: str | None) -> str | Response:
        """Borde: actor y capacidad vigente antes de llamar a Terceros (ADR-024)."""
        caller = authenticate_athlete(authorization)
        if isinstance(caller, Response):
            return caller
        user_id, token = caller
        try:
            snapshot = athlete_snapshot(user_id)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _failure(error)
        granted = snapshot.get("capabilities") or []
        if snapshot.get("subscription_status") == "ACTIVE" and STRAVA_CAPABILITY in granted:
            return token
        return error_response(
            HTTPStatus.FORBIDDEN, "capability_required", "Su plan no incluye esta funcionalidad."
        )

    def relay(
        authorization: str | None,
        send: Callable[[str], httpx.Response],
        allowed: set[int],
    ) -> Response:
        token = authorized_token(authorization)
        if isinstance(token, Response):
            return token
        try:
            upstream = send(token)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _failure(error)
        return _passthrough(upstream, allowed)

    @router.post(
        "/v1/me/integrations/strava/authorization",
        status_code=HTTPStatus.CREATED,
        response_model=StravaAuthorizationResponse,
        summary="Iniciar la vinculación con Strava (HU031)",
        responses={**_edge(), HTTPStatus.CREATED.value: {"headers": _NO_STORE}},
    )
    def start_strava_authorization(
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        response = relay(
            authorization,
            third_party.start_strava_authorization,
            {HTTPStatus.CREATED, *_EDGE_STATUSES},
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get(
        "/v1/me/integrations/strava",
        response_model=StravaIntegrationResponse,
        summary="Estado de mi vinculación con Strava y de la última sincronización (HU031)",
        responses={**_edge(), HTTPStatus.OK.value: {"headers": _NO_STORE}},
    )
    def strava_integration(authorization: Annotated[str | None, Header()] = None) -> Response:
        response = relay(
            authorization, third_party.strava_integration, {HTTPStatus.OK, *_EDGE_STATUSES}
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.delete(
        "/v1/me/integrations/strava",
        status_code=HTTPStatus.NO_CONTENT,
        response_class=Response,
        summary="Desvincular Strava (HU031)",
        description="Idempotente: revoca en Strava (mejor esfuerzo), borra los tokens, cancela "
        "las corridas pendientes y conserva las actividades ya importadas.",
        responses=_edge(),
    )
    def unlink_strava(authorization: Annotated[str | None, Header()] = None) -> Response:
        return relay(
            authorization, third_party.unlink_strava, {HTTPStatus.NO_CONTENT, *_EDGE_STATUSES}
        )

    @router.post(
        "/v1/me/integrations/strava/syncs",
        status_code=HTTPStatus.ACCEPTED,
        response_model=StravaSyncResponse,
        summary="Solicitar una sincronización manual con Strava (HU031)",
        responses={
            **_edge(),
            HTTPStatus.ACCEPTED.value: {"headers": _REPLAY},
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`sync_in_progress` (ya hay una corrida `QUEUED`/`RUNNING`) o "
                "`integration_not_linked` (sin vinculación `ACTIVE`).",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `Idempotency-Key` ausente o inválida.",
            },
        },
    )
    def request_strava_sync(
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        return relay(
            authorization,
            lambda token: third_party.request_strava_sync(token, idempotency_key),
            {
                HTTPStatus.ACCEPTED,
                HTTPStatus.CONFLICT,
                HTTPStatus.UNPROCESSABLE_ENTITY,
                *_EDGE_STATUSES,
            },
        )

    @router.get(
        "/v1/integrations/strava/callback",
        status_code=HTTPStatus.FOUND,
        response_class=RedirectResponse,
        summary="Callback de autorización de Strava (público, HU031)",
        description="`redirect_uri` registrado en Strava; sin token de SportApp. Siempre "
        "redirige al enlace profundo de la aplicación móvil con el resultado.",
        responses={
            HTTPStatus.FOUND.value: {
                "description": "Redirección al enlace profundo con el resultado.",
                "headers": _REDIRECT,
            }
        },
    )
    def strava_callback(
        code: Annotated[
            str | None, _callback_parameter("Código de autorización de Strava.")
        ] = None,
        state: Annotated[
            str | None, _callback_parameter("Estado antifalsificación emitido por SportApp.")
        ] = None,
        scope: Annotated[
            str | None, _callback_parameter("Scopes aceptados, separados por comas.")
        ] = None,
        error: Annotated[
            str | None, _callback_parameter("Motivo del rechazo (p. ej. `access_denied`).")
        ] = None,
    ) -> Response:
        parameters = {"code": code, "state": state, "scope": scope, "error": error}
        try:
            result = _link_result(third_party.complete_strava_authorization(parameters))
        except (UpstreamTimeout, UpstreamUnavailable) as failure:
            _logger.warning(
                "bff.upstream_unavailable",
                extra={"fields": {"upstream": UPSTREAM, "reason": failure.code}},
            )
            result = LINK_ERROR
        _logger.info("strava.callback", extra={"fields": {"result": result}})
        return RedirectResponse(
            f"{DEEP_LINK}?result={result}",
            status_code=HTTPStatus.FOUND,
            headers={"Cache-Control": "no-store"},
        )

    return router
