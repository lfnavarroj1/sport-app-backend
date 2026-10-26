"""HU039: bandeja, preferencias y token *push* del deportista (ADR-045 §11).

- `GET /v1/me/notifications?limit&cursor`: bandeja propia (`notification_inbox_view`) en
  orden `created_at DESC, notification_id DESC`.
- `POST /v1/me/notifications/{notification_id}/read`: marca como leída (idempotente).
- `GET`/`PUT /v1/me/notification-preferences`: `event_matches`, `event_reminders`,
  `push_enabled` y `version`; el `PUT` exige `expected_version` (`409 version_conflict`).
- `PUT /v1/me/push-tokens`: registra el token FCM del dispositivo (`204`); sin
  `FCM_ENABLED` responde `409 push_not_enabled`.
Solo `athlete`, sin capacidad de plan; el deportista sale del token. Los cuerpos se leen como
JSON sin validación automática para responder `422 validation_error` con
`details[{field, issue}]`; su esquema se documenta en el OpenAPI. Instantes en UTC (`Z`).
"""

from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Header, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from events_management.api.identity import AUTH_RESPONSES, TokenVerifier, authenticate, forbidden
from events_management.application.commands.notification_settings import (
    ChangeNotificationPreferences,
    GetNotificationPreferences,
    PreferencesChange,
    RegisterPushToken,
)
from events_management.application.errors import (
    NotificationNotFound,
    PermissionRequired,
    PushNotEnabled,
)
from events_management.application.ports import Caller, InboxItem, StoreUnavailable
from events_management.application.queries.notifications import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    InvalidNotificationsQuery,
    ListMyNotifications,
    MarkNotificationRead,
)
from events_management.domain.notifications.model import (
    NotificationPreferences,
    VersionConflict,
)
from events_management.domain.sport_events.created import utc_instant
from events_management.domain.validation import Issue, Violation
from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response

PREFERENCE_FLAGS = ("event_matches", "event_reminders", "push_enabled")
PREFERENCE_FIELDS = (*PREFERENCE_FLAGS, "expected_version")
PUSH_TOKEN_FIELDS = ("token", "platform")
PLATFORMS = ("android",)
TOKEN_MAX = 4096


class NotificationResponse(BaseModel):
    notification_id: UUID
    type: Literal["EVENT_PUBLISHED_MATCH", "EVENT_REMINDER_24H"]
    priority: Literal["NORMAL", "HIGH"]
    event_id: Annotated[UUID, Field(description="Al tocarla, la app abre el detalle (HU041).")]
    title: str
    body: str
    created_at: Annotated[str, Field(description="Instante UTC (`Z`).")]
    read_at: Annotated[str | None, Field(description="Instante UTC (`Z`); `null` sin leer.")]


class NotificationsResponse(BaseModel):
    items: list[NotificationResponse]
    next_cursor: str | None


class NotificationPreferencesResponse(BaseModel):
    event_matches: Annotated[bool, Field(description="Eventos publicados que coinciden.")]
    event_reminders: Annotated[bool, Field(description="Recordatorio 24 h antes.")]
    push_enabled: Annotated[bool, Field(description="*Push* (solo con FCM habilitado).")]
    version: Annotated[int, Field(description="`0` mientras rigen los valores por defecto.")]


class NotificationPreferencesRequest(BaseModel):
    """Esquema documentado del cuerpo; la validación la hace el transporte."""

    model_config = ConfigDict(extra="forbid")

    event_matches: bool
    event_reminders: bool
    push_enabled: bool
    expected_version: Annotated[
        int, Field(ge=0, description="Versión vigente que vio el deportista (`0` la primera).")
    ]


class PushTokenRequest(BaseModel):
    """Esquema documentado del cuerpo; la validación la hace el transporte."""

    model_config = ConfigDict(extra="forbid")

    token: Annotated[str, Field(min_length=1, max_length=TOKEN_MAX, description="Token FCM.")]
    platform: Literal["android"]


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    schema.pop("$defs", None)
    return schema


def _body(model: type[BaseModel]) -> dict[str, Any]:
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _schema(model)}},
        }
    }


def notification_document(item: InboxItem) -> dict[str, Any]:
    return {
        "notification_id": str(item.notification_id),
        "type": item.type,
        "priority": item.priority,
        "event_id": str(item.event_id),
        "title": item.title,
        "body": item.body,
        "created_at": utc_instant(item.created_at),
        "read_at": utc_instant(item.read_at) if item.read_at else None,
    }


def preferences_document(preferences: NotificationPreferences) -> dict[str, Any]:
    return {
        "event_matches": preferences.event_matches,
        "event_reminders": preferences.event_reminders,
        "push_enabled": preferences.push_enabled,
        "version": preferences.version,
    }


def _invalid(violations: list[Violation]) -> Response:
    return error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "validation_error",
        "La solicitud no es válida.",
        [ErrorDetail(field=item.field, issue=item.issue.value) for item in violations],
    )


def _object(
    body: object, allowed: tuple[str, ...], violations: list[Violation]
) -> dict[str, Any] | None:
    if not isinstance(body, dict):
        violations.append(Violation(None, Issue.REQUIRED if body is None else Issue.UNKNOWN_VALUE))
        return None
    violations.extend(
        Violation(name, Issue.FIELD_NOT_ALLOWED) for name in body if name not in allowed
    )
    return body


def parse_preferences(body: object) -> PreferencesChange | list[Violation]:
    violations: list[Violation] = []
    document = _object(body, PREFERENCE_FIELDS, violations)
    if document is None:
        return violations
    flags: dict[str, bool] = {}
    for name in PREFERENCE_FLAGS:
        value = document.get(name)
        if value is None:
            violations.append(Violation(name, Issue.REQUIRED))
        elif not isinstance(value, bool):
            violations.append(Violation(name, Issue.UNKNOWN_VALUE))
        else:
            flags[name] = value
    version = document.get("expected_version")
    if version is None:
        violations.append(Violation("expected_version", Issue.REQUIRED))
    elif not isinstance(version, int) or isinstance(version, bool):
        violations.append(Violation("expected_version", Issue.UNKNOWN_VALUE))
    elif version < 0:
        violations.append(Violation("expected_version", Issue.OUT_OF_RANGE))
    if violations or not isinstance(version, int):
        return violations
    return PreferencesChange(expected_version=version, **flags)


def parse_push_token(body: object) -> tuple[str, str] | list[Violation]:
    violations: list[Violation] = []
    document = _object(body, PUSH_TOKEN_FIELDS, violations)
    if document is None:
        return violations
    token = document.get("token")
    if token is None:
        violations.append(Violation("token", Issue.REQUIRED))
    elif not isinstance(token, str):
        violations.append(Violation("token", Issue.UNKNOWN_VALUE))
    elif not 1 <= len(token) <= TOKEN_MAX or token != token.strip():
        violations.append(Violation("token", Issue.INVALID_LENGTH))
    platform = document.get("platform")
    if platform is None:
        violations.append(Violation("platform", Issue.REQUIRED))
    elif platform not in PLATFORMS:
        violations.append(Violation("platform", Issue.UNKNOWN_VALUE))
    if violations:
        return violations
    return str(token), str(platform)


_NO_STORE = {"Cache-Control": "no-store"}
_ATHLETE_RESPONSES: dict[int | str, dict[str, Any]] = {
    **AUTH_RESPONSES,
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required`: solo el deportista.",
    },
    HTTPStatus.SERVICE_UNAVAILABLE.value: {
        "model": ErrorResponse,
        "description": f"`{StoreUnavailable.code}`: no se pudo verificar la identidad o la "
        "base del servicio no responde.",
    },
}


_ERRORS = (
    PermissionRequired,
    StoreUnavailable,
    InvalidNotificationsQuery,
    NotificationNotFound,
    VersionConflict,
    PushNotEnabled,
)


def _failure(error: Exception) -> Response:
    """Respuesta de cada error de la aplicación con su código estable."""
    if isinstance(error, PermissionRequired):
        return forbidden(error)
    if isinstance(error, StoreUnavailable):
        return error_response(
            HTTPStatus.SERVICE_UNAVAILABLE,
            error.code,
            "No fue posible completar la operación. Intente de nuevo.",
        )
    if isinstance(error, InvalidNotificationsQuery):
        return error_response(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            error.code,
            "La solicitud no es válida.",
            [ErrorDetail(field=f"query.{error.field}", issue=error.issue)],
        )
    if isinstance(error, NotificationNotFound):
        return error_response(
            HTTPStatus.NOT_FOUND, error.code, "La notificación no existe o no está disponible."
        )
    if isinstance(error, VersionConflict):
        return error_response(
            HTTPStatus.CONFLICT,
            error.code,
            "Las preferencias cambiaron desde que las consultó. Vuelva a cargarlas.",
            [ErrorDetail(field="expected_version", issue=str(error.current_version))],
        )
    return error_response(
        HTTPStatus.CONFLICT, PushNotEnabled.code, "Las notificaciones push no están habilitadas."
    )


def _athlete(verify_token: TokenVerifier, authorization: str | None) -> Caller | Response:
    """Identidad verificada del deportista o la respuesta de error (`401`, `403`, `503`)."""
    caller = authenticate(verify_token, authorization)
    if isinstance(caller, Response):
        return caller
    if caller.actor_type != "athlete":
        return forbidden(PermissionRequired())
    return caller


def _limit(raw: str | None) -> int | None:
    """`limit` como texto: un valor no entero es `out_of_range`, igual que fuera del rango
    (HU034/HU014), y se valida después de autenticar."""
    if raw is None:
        return DEFAULT_LIMIT
    text = raw.strip()
    return int(text) if text.isascii() and text.isdigit() else None


def _validation(description: str) -> dict[int | str, dict[str, Any]]:
    return {
        HTTPStatus.UNPROCESSABLE_ENTITY.value: {
            "model": ErrorResponse,
            "description": f"`validation_error` con `details[{{field, issue}}]`: {description}",
        }
    }


def build_notifications_router(
    *,
    verify_token: TokenVerifier,
    list_notifications: ListMyNotifications,
    mark_read: MarkNotificationRead,
    get_preferences: GetNotificationPreferences,
    change_preferences: ChangeNotificationPreferences,
    register_push_token: RegisterPushToken,
) -> APIRouter:
    router = APIRouter(prefix="/v1/me", tags=["notifications"])

    @router.get(
        "/notifications",
        response_model=NotificationsResponse,
        summary="Mi bandeja de notificaciones (HU039)",
        description="`EVENT_PUBLISHED_MATCH` (`NORMAL`) y `EVENT_REMINDER_24H` (`HIGH`) en "
        f"orden `created_at DESC, notification_id DESC`; `limit` de 1 a {MAX_LIMIT}. La "
        "bandeja es eventualmente consistente con la generación.",
        responses={
            **_ATHLETE_RESPONSES,
            **_validation(
                "`query.limit` (`out_of_range`, también si no es entero) o `query.cursor` "
                "(`invalid_cursor`, también si supera 512 caracteres)."
            ),
        },
    )
    def my_notifications(
        *,
        limit: Annotated[
            str | None,
            Query(description=f"Entero de 1 a {MAX_LIMIT} (por defecto {DEFAULT_LIMIT})."),
        ] = None,
        cursor: Annotated[
            str | None, Query(description="`next_cursor` de la página anterior.")
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        caller = _athlete(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        parsed_limit = _limit(limit)
        try:
            if parsed_limit is None:
                raise InvalidNotificationsQuery("limit", "out_of_range")
            page = list_notifications(caller, limit=parsed_limit, cursor=cursor)
        except _ERRORS as error:
            return _failure(error)
        body = {
            "items": [notification_document(item) for item in page.items],
            "next_cursor": page.next_cursor,
        }
        return JSONResponse(body, headers=_NO_STORE)

    @router.post(
        "/notifications/{notification_id}/read",
        response_model=NotificationResponse,
        summary="Marcar una notificación como leída (HU039)",
        description="Idempotente: repetirla conserva el primer `read_at`.",
        responses={
            **_ATHLETE_RESPONSES,
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": "`notification_not_found`: inexistente, ajena o aún no "
                "disponible en la bandeja.",
            },
            **_validation("`path.notification_id` (`unknown_value`) no es un UUID."),
        },
    )
    def read_notification(
        notification_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        caller = _athlete(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            parsed = UUID(notification_id)
        except ValueError:
            return _invalid([Violation("path.notification_id", Issue.UNKNOWN_VALUE)])
        try:
            item = mark_read(caller, parsed)
        except _ERRORS as error:
            return _failure(error)
        return JSONResponse(notification_document(item), headers=_NO_STORE)

    @router.get(
        "/notification-preferences",
        response_model=NotificationPreferencesResponse,
        summary="Mis preferencias de notificación (HU039)",
        description="Sin preferencias registradas rigen los valores por defecto (todo "
        "habilitado) con `version: 0`.",
        responses=_ATHLETE_RESPONSES,
    )
    def my_preferences(authorization: Annotated[str | None, Header()] = None) -> Response:
        caller = _athlete(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            preferences = get_preferences(caller)
        except _ERRORS as error:
            return _failure(error)
        return JSONResponse(preferences_document(preferences), headers=_NO_STORE)

    @router.put(
        "/notification-preferences",
        response_model=NotificationPreferencesResponse,
        summary="Cambiar mis preferencias de notificación (HU039)",
        description="Reemplaza las tres preferencias con control optimista por "
        "`expected_version`; la respuesta trae la versión nueva. Un tipo deshabilitado deja "
        "de generarse (CA2).",
        openapi_extra=_body(NotificationPreferencesRequest),
        responses={
            **_ATHLETE_RESPONSES,
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`version_conflict` (`details[0]` = `{field: expected_version, "
                "issue: <versión vigente>}`).",
            },
            **_validation(
                "`event_matches`, `event_reminders`, `push_enabled` (`required`, "
                "`unknown_value`), `expected_version` (`required`, `unknown_value`, "
                "`out_of_range`) o campos no permitidos (`field_not_allowed`)."
            ),
        },
    )
    def put_preferences(
        body: Annotated[Any, Body()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        caller = _athlete(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        change = parse_preferences(body)
        if isinstance(change, list):
            return _invalid(change)
        try:
            preferences = change_preferences(caller, change)
        except _ERRORS as error:
            return _failure(error)
        return JSONResponse(preferences_document(preferences), headers=_NO_STORE)

    @router.put(
        "/push-tokens",
        status_code=HTTPStatus.NO_CONTENT,
        response_class=Response,
        summary="Registrar el token push del dispositivo (HU039, solo con FCM)",
        description="El token se guarda cifrado; si pertenecía a otro deportista se le "
        "reasigna. La bandeja no depende del *push*.",
        openapi_extra=_body(PushTokenRequest),
        responses={
            **_ATHLETE_RESPONSES,
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`push_not_enabled`: FCM no está habilitado en este ambiente.",
            },
            **_validation(
                "`token` (`required`, `unknown_value`, `invalid_length`), `platform` "
                "(`required`, `unknown_value`) o campos no permitidos (`field_not_allowed`)."
            ),
        },
    )
    def put_push_token(
        body: Annotated[Any, Body()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        caller = _athlete(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        parsed = parse_push_token(body)
        if isinstance(parsed, list):
            return _invalid(parsed)
        try:
            register_push_token(caller, *parsed)
        except _ERRORS as error:
            return _failure(error)
        return Response(status_code=HTTPStatus.NO_CONTENT)

    return router
