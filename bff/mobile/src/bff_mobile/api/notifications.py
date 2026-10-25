"""HU039 en el canal móvil: bandeja de notificaciones, preferencias y token push (ADR-045 §11).

El BFF autentica (JWKS), filtra por actor `athlete` y reenvía el token, la correlación, los
parámetros y el cuerpo (bytes tal cual, sin interpretar el JSON) a `events_management`, que
vuelve a verificar la identidad y decide reglas y errores: `expected_version` de las
preferencias, `push_not_enabled` sin FCM, notificación inexistente o ajena. Las respuestas
llegan sin alterar; un estado no previsto es `upstream_unavailable`.

El deportista sale del token; ningún título, cuerpo de notificación ni token de dispositivo
se escribe en los logs.
"""

import logging
from collections.abc import Callable
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.concurrency import run_in_threadpool

from bff_mobile.clients.events_management import EventsManagementClient
from bff_mobile.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.errors import ErrorResponse, error_response
from sportapp_platform.tokens import InvalidAccessToken, JwksUnavailable, VerifiedAccessToken

ATHLETE = "athlete"
BEARER_PREFIX = "bearer "
INSTANT = "Instante UTC con sufijo `Z`; la app lo convierte a la zona del dispositivo."
# Cabeceras del servicio que el cliente necesita tal como llegan.
RELAYED_HEADERS = ("Cache-Control", "WWW-Authenticate")

_logger = logging.getLogger("sportapp.bff")

NotificationType = Literal["EVENT_PUBLISHED_MATCH", "EVENT_REMINDER_24H"]
NotificationPriority = Literal["NORMAL", "HIGH"]


class NotificationResponse(BaseModel):
    """Notificación de la bandeja: `EVENT_PUBLISHED_MATCH` (`NORMAL`) o `EVENT_REMINDER_24H`
    (`HIGH`)."""

    notification_id: UUID
    type: NotificationType
    priority: NotificationPriority
    event_id: Annotated[UUID, Field(description="Evento del detalle (HU041).")]
    title: str
    body: str
    created_at: Annotated[str, Field(description=INSTANT)]
    read_at: Annotated[str | None, Field(description=f"{INSTANT} `null` mientras no se lea.")]


class NotificationsPageResponse(BaseModel):
    """Página de la bandeja, `created_at DESC, notification_id DESC`."""

    items: list[NotificationResponse]
    next_cursor: Annotated[str | None, Field(description="`null` en la última página.")]


class NotificationPreferencesResponse(BaseModel):
    event_matches: Annotated[bool, Field(description="Eventos publicados que coinciden.")]
    event_reminders: Annotated[bool, Field(description="Recordatorios 24 h antes del inicio.")]
    push_enabled: Annotated[bool, Field(description="Entrega push además de la bandeja.")]
    version: Annotated[int, Field(description="`0` mientras rigen los valores por defecto.")]


class NotificationPreferencesRequest(BaseModel):
    """Esquema documentado del `PUT`; la validación la hace el servicio."""

    model_config = ConfigDict(extra="forbid")

    event_matches: bool
    event_reminders: bool
    push_enabled: bool
    expected_version: Annotated[
        int, Field(ge=0, description="Versión leída; `0` si nunca se configuraron.")
    ]


class PushTokenRequest(BaseModel):
    """Esquema documentado del registro del token FCM; la validación la hace el servicio."""

    model_config = ConfigDict(extra="forbid")

    token: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    platform: Literal["android"]


def _request_body(model: type[BaseModel]) -> dict[str, Any]:
    """Cuerpo documentado autocontenido: resuelve las referencias internas (`$defs`)."""
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            reference = node.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                return resolve(definitions[reference.removeprefix("#/$defs/")])
            return {name: resolve(value) for name, value in node.items()}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": resolve(schema)}},
        }
    }


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


def _unexpected(status: int) -> Response:
    _logger.warning(
        "bff.upstream_unexpected_status",
        extra={"fields": {"upstream": "events-management", "status": status}},
    )
    return _failure(UpstreamUnavailable())


def _passthrough(upstream: httpx.Response, allowed: set[int]) -> Response:
    if upstream.status_code not in allowed:
        return _unexpected(upstream.status_code)
    headers = {name: upstream.headers[name] for name in RELAYED_HEADERS if name in upstream.headers}
    if upstream.status_code == HTTPStatus.NO_CONTENT:
        return Response(status_code=HTTPStatus.NO_CONTENT, headers=headers)
    try:
        content = upstream.json()
    except ValueError:
        return _unexpected(upstream.status_code)
    return JSONResponse(status_code=upstream.status_code, content=content, headers=headers)


_COMMON: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNAUTHORIZED.value: {
        "model": ErrorResponse,
        "description": "`invalid_access_token`: falta el token o no es válido.",
    },
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required`: solo el deportista.",
    },
    HTTPStatus.SERVICE_UNAVAILABLE.value: {
        "model": ErrorResponse,
        "description": "`upstream_unavailable`: JWKS, base o servicio no disponibles.",
    },
    HTTPStatus.GATEWAY_TIMEOUT.value: {
        "model": ErrorResponse,
        "description": "`upstream_timeout`.",
    },
}
# Estados del borde y del servicio que se entregan tal cual en todas las operaciones.
_COMMON_STATUSES = {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN, HTTPStatus.SERVICE_UNAVAILABLE}


def build_notifications_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    events: EventsManagementClient,
) -> APIRouter:
    router = APIRouter(tags=["notifications"])

    def athlete_token(authorization: str | None) -> str | Response:
        if not authorization or not authorization.lower().startswith(BEARER_PREFIX):
            return _unauthenticated()
        token = authorization[len(BEARER_PREFIX) :].strip()
        try:
            identity = verify_token(token)
        except InvalidAccessToken:
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
        return token

    def relay(
        authorization: str | None,
        send: Callable[[str], httpx.Response],
        allowed: set[HTTPStatus],
    ) -> Response:
        token = athlete_token(authorization)
        if isinstance(token, Response):
            return token
        try:
            upstream = send(token)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _failure(error)
        return _passthrough(upstream, {*allowed, *_COMMON_STATUSES})

    @router.get(
        "/v1/me/notifications",
        response_model=NotificationsPageResponse,
        summary="Mi bandeja de notificaciones de eventos (HU039)",
        responses={
            **_COMMON,
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `limit` fuera de 1-50 o `cursor` inválido.",
            },
        },
    )
    def list_notifications(
        *,
        limit: Annotated[str | None, Query(description="1 a 50; por defecto 20.")] = None,
        cursor: Annotated[str | None, Query(description="Opaco, hasta 512 caracteres.")] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        params = {
            name: value
            for name, value in (("limit", limit), ("cursor", cursor))
            if value is not None
        }
        return relay(
            authorization,
            lambda token: events.notifications(token, params),
            {HTTPStatus.OK, HTTPStatus.UNPROCESSABLE_ENTITY},
        )

    @router.post(
        "/v1/me/notifications/{notification_id}/read",
        response_model=NotificationResponse,
        summary="Marcar una notificación como leída (HU039)",
        description="Idempotente: una notificación ya leída conserva su `read_at`.",
        responses={
            **_COMMON,
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": "`notification_not_found`: inexistente, ajena o aún no "
                "disponible en la bandeja.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `notification_id` no es un UUID.",
            },
        },
    )
    def mark_read(
        notification_id: str, authorization: Annotated[str | None, Header()] = None
    ) -> Response:
        return relay(
            authorization,
            lambda token: events.mark_notification_read(token, notification_id),
            {HTTPStatus.OK, HTTPStatus.NOT_FOUND, HTTPStatus.UNPROCESSABLE_ENTITY},
        )

    @router.get(
        "/v1/me/notification-preferences",
        response_model=NotificationPreferencesResponse,
        summary="Mis preferencias de notificación (HU039)",
        responses=_COMMON,
    )
    def get_preferences(authorization: Annotated[str | None, Header()] = None) -> Response:
        return relay(authorization, events.notification_preferences, {HTTPStatus.OK})

    @router.put(
        "/v1/me/notification-preferences",
        response_model=NotificationPreferencesResponse,
        summary="Reemplazar mis preferencias de notificación (HU039)",
        description="Control optimista: `expected_version` debe ser la versión vigente; la "
        "respuesta trae la siguiente.",
        openapi_extra=_request_body(NotificationPreferencesRequest),
        responses={
            **_COMMON,
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": '`version_conflict` con `details=[{field: "expected_version", '
                'issue: "<versión vigente>"}]`.',
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` con `details[{field, issue}]`: campo "
                "ausente, de más o con un valor no válido, o cuerpo ausente.",
            },
        },
    )
    async def put_preferences(
        request: Request, authorization: Annotated[str | None, Header()] = None
    ) -> Response:
        # Bytes crudos: el BFF no interpreta el JSON (ni siquiera uno mal formado) y
        # autentica antes de cualquier validación; lo bloqueante corre en el pool.
        body = await request.body()
        return await run_in_threadpool(
            relay,
            authorization,
            lambda token: events.replace_notification_preferences(token, body),
            {HTTPStatus.OK, HTTPStatus.CONFLICT, HTTPStatus.UNPROCESSABLE_ENTITY},
        )

    @router.put(
        "/v1/me/push-tokens",
        status_code=HTTPStatus.NO_CONTENT,
        response_class=Response,
        summary="Registrar el token push del dispositivo (HU039)",
        description="Asocia el token FCM al deportista del token de acceso; si pertenecía a "
        "otro deportista, se reasigna.",
        openapi_extra=_request_body(PushTokenRequest),
        responses={
            **_COMMON,
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`push_not_enabled`: la entrega push no está habilitada.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` con `details[{field, issue}]`: `token` "
                "ausente o de longitud no válida, `platform` distinta de `android`, campo de "
                "más o cuerpo ausente.",
            },
        },
    )
    async def put_push_token(
        request: Request, authorization: Annotated[str | None, Header()] = None
    ) -> Response:
        body = await request.body()
        return await run_in_threadpool(
            relay,
            authorization,
            lambda token: events.register_push_token(token, body),
            {HTTPStatus.NO_CONTENT, HTTPStatus.CONFLICT, HTTPStatus.UNPROCESSABLE_ENTITY},
        )

    return router
