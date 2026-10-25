"""HU038 (ADR-045 §4-§5): reglas y preferencias de los recordatorios durante la sesión.

- `GET /v1/session-reminder-rules`: ruleset versionado `session-reminders-2026-10-13.v1`
  (`HYDRATION` y `NUTRITION` con intervalo por defecto, rango, `applies_after_minutes`,
  `message` aprobado y `disclaimer`). Es configuración en código e inmutable por versión.
- `GET`/`PUT /v1/me/session-reminder-preferences`: preferencias del deportista del token con
  control de versión (`expected_version`; `409 version_conflict` con la vigente). Sin
  configuración, los valores por defecto del ruleset con `version: 0`.

Solo `athlete` con `nutrition.session.reminders`. El backend no evalúa ni envía recordatorios:
lo hace el dispositivo. El cuerpo del `PUT` se lee como JSON sin validación automática para
responder `details[{field, issue}]`; su esquema se documenta en el OpenAPI. Las respuestas de
preferencias salen con `Cache-Control: no-store`.
"""

from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, Response
from pydantic import BaseModel, ConfigDict, Field

from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from training_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    forbidden,
)
from training_management.application.access import SESSION_REMINDERS_CAPABILITY
from training_management.application.commands.session_reminder_preferences import (
    UpdateSessionReminderPreferences,
)
from training_management.application.errors import (
    CapabilitiesTimeout,
    CapabilitiesUnavailable,
    CapabilityRequired,
    PermissionRequired,
)
from training_management.application.queries.session_reminders import (
    GetSessionReminderPreferences,
    GetSessionReminderRules,
)
from training_management.domain.reminders.model import (
    RULESET,
    RULESET_VERSION,
    InvalidReminderPreferences,
    ReminderPreferences,
    ReminderPreferencesVersionConflict,
    ReminderRule,
    ReminderRuleset,
    ReminderSetting,
    ReminderType,
)

_HYDRATION = RULESET.rule(ReminderType.HYDRATION)
_NUTRITION = RULESET.rule(ReminderType.NUTRITION)


class ReminderRuleResponse(BaseModel):
    type: ReminderType
    default_interval_minutes: Annotated[int, Field(description="Intervalo por defecto.")]
    min: Annotated[int, Field(description="Intervalo mínimo configurable, en minutos.")]
    max: Annotated[int, Field(description="Intervalo máximo configurable, en minutos.")]
    applies_after_minutes: Annotated[
        int,
        Field(
            description="La regla aplica cuando la sesión supera esta duración (`0`: desde el "
            "inicio; `NUTRITION`: más de 60 min)."
        ),
    ]
    message: Annotated[str, Field(description="Texto aprobado; el cliente no lo modifica.")]
    disclaimer: Annotated[str, Field(description="Aviso: no diagnostica ni prescribe.")]

    @classmethod
    def from_rule(cls, rule: ReminderRule) -> "ReminderRuleResponse":
        return cls(
            type=rule.type,
            default_interval_minutes=rule.default_interval_minutes,
            min=rule.min,
            max=rule.max,
            applies_after_minutes=rule.applies_after_minutes,
            message=rule.message,
            disclaimer=rule.disclaimer,
        )


class ReminderRulesResponse(BaseModel):
    ruleset_version: Annotated[str, Field(examples=[RULESET_VERSION])]
    rules: list[ReminderRuleResponse]

    @classmethod
    def from_ruleset(cls, ruleset: ReminderRuleset) -> "ReminderRulesResponse":
        return cls(
            ruleset_version=ruleset.ruleset_version,
            rules=[ReminderRuleResponse.from_rule(rule) for rule in ruleset.rules],
        )


class HydrationSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    interval_minutes: Annotated[
        int,
        Field(
            ge=_HYDRATION.min,
            le=_HYDRATION.max,
            description=f"Minutos entre recordatorios ({_HYDRATION.min}-{_HYDRATION.max}).",
        ),
    ]


class NutritionSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    interval_minutes: Annotated[
        int,
        Field(
            ge=_NUTRITION.min,
            le=_NUTRITION.max,
            description=f"Minutos entre recordatorios ({_NUTRITION.min}-{_NUTRITION.max}).",
        ),
    ]


class ReminderPreferencesRequest(BaseModel):
    """Esquema documentado del `PUT`; la validación la hace el dominio."""

    model_config = ConfigDict(extra="forbid")

    hydration: HydrationSetting
    nutrition: NutritionSetting
    expected_version: Annotated[
        int, Field(ge=0, description="Versión leída; `0` si aún no hay preferencias guardadas.")
    ]


class ReminderPreferencesResponse(BaseModel):
    hydration: HydrationSetting
    nutrition: NutritionSetting
    version: Annotated[
        int,
        Field(
            ge=0,
            description="`0` sin preferencias guardadas (valores por defecto del ruleset); +1 "
            "en cada cambio.",
        ),
    ]

    @classmethod
    def from_preferences(cls, preferences: ReminderPreferences) -> "ReminderPreferencesResponse":
        def setting(value: ReminderSetting) -> dict[str, Any]:
            return {"enabled": value.enabled, "interval_minutes": value.interval_minutes}

        return cls.model_validate(
            {
                "hydration": setting(preferences.hydration),
                "nutrition": setting(preferences.nutrition),
                "version": preferences.version,
            }
        )


_MESSAGES: dict[type[Exception], tuple[HTTPStatus, str]] = {
    CapabilitiesTimeout: (
        HTTPStatus.GATEWAY_TIMEOUT,
        "No fue posible verificar su plan a tiempo. Intente de nuevo.",
    ),
    CapabilitiesUnavailable: (
        HTTPStatus.SERVICE_UNAVAILABLE,
        "No fue posible verificar su plan. Intente de nuevo.",
    ),
}
_HANDLED: tuple[type[Exception], ...] = (
    PermissionRequired,
    CapabilityRequired,
    InvalidReminderPreferences,
    ReminderPreferencesVersionConflict,
    *_MESSAGES,
)
_EDGE: dict[int | str, dict[str, Any]] = {
    **AUTH_RESPONSES,
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required` (no es deportista) o `capability_required` "
        f"(`{SESSION_REMINDERS_CAPABILITY}`).",
    },
    HTTPStatus.SERVICE_UNAVAILABLE.value: {
        "model": ErrorResponse,
        "description": "`upstream_unavailable`: no se pudo verificar identidad o el plan.",
    },
    HTTPStatus.GATEWAY_TIMEOUT.value: {
        "model": ErrorResponse,
        "description": "`upstream_timeout`: Afiliación no respondió a tiempo.",
    },
}


def _failure(error: Exception) -> Response:
    if isinstance(error, PermissionRequired | CapabilityRequired):
        return forbidden(error)
    if isinstance(error, InvalidReminderPreferences):
        return error_response(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            error.code,
            "La solicitud no es válida.",
            [ErrorDetail(field=field, issue=issue) for field, issue in error.violations],
        )
    if isinstance(error, ReminderPreferencesVersionConflict):
        return error_response(
            HTTPStatus.CONFLICT,
            error.code,
            "Las preferencias cambiaron desde la versión indicada.",
            [ErrorDetail(field="expected_version", issue=str(error.current_version))],
        )
    status, message = _MESSAGES[type(error)]
    return error_response(status, getattr(error, "code", ""), message)


def _body_schema() -> dict[str, Any]:
    schema = ReminderPreferencesRequest.model_json_schema()
    definitions = schema.pop("$defs", {})
    for name, value in schema["properties"].items():
        reference = value.get("$ref", "")
        if reference.startswith("#/$defs/"):
            schema["properties"][name] = definitions[reference.removeprefix("#/$defs/")]
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": schema}},
        }
    }


def build_session_reminders_router(
    *,
    verify_token: TokenVerifier,
    get_rules: GetSessionReminderRules,
    get_preferences: GetSessionReminderPreferences,
    update_preferences: UpdateSessionReminderPreferences,
) -> APIRouter:
    router = APIRouter(tags=["training-session-reminders"])

    @router.get(
        "/v1/session-reminder-rules",
        response_model=ReminderRulesResponse,
        summary="Reglas versionadas de recordatorios durante la sesión (HU038)",
        description="Ruleset `session-reminders-2026-10-13.v1`: el dispositivo evalúa las "
        "reglas sobre el cronómetro local de la sesión activa. Sin cantidades ni prescripción.",
        responses=_EDGE,
    )
    def get_session_reminder_rules(
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | ReminderRulesResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            ruleset = get_rules(caller)
        except _HANDLED as error:
            return _failure(error)
        return ReminderRulesResponse.from_ruleset(ruleset)

    @router.get(
        "/v1/me/session-reminder-preferences",
        response_model=ReminderPreferencesResponse,
        summary="Mis preferencias de recordatorios durante la sesión (HU038)",
        description="Sin preferencias guardadas devuelve los valores por defecto del ruleset "
        "con ambos tipos habilitados y `version: 0`.",
        responses=_EDGE,
    )
    def get_session_reminder_preferences(
        response: Response, authorization: Annotated[str | None, Header()] = None
    ) -> Response | ReminderPreferencesResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            preferences = get_preferences(caller)
        except _HANDLED as error:
            return _failure(error)
        response.headers["Cache-Control"] = "no-store"
        return ReminderPreferencesResponse.from_preferences(preferences)

    @router.put(
        "/v1/me/session-reminder-preferences",
        response_model=ReminderPreferencesResponse,
        summary="Configurar mis recordatorios durante la sesión (HU038)",
        description="Reemplaza las preferencias con `expected_version` (la versión leída). "
        "Repetir los mismos valores no cambia la versión.",
        openapi_extra=_body_schema(),
        responses={
            **_EDGE,
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`version_conflict`: `details[0]` = `{field: expected_version, "
                "issue: <versión vigente>}`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `required`, `unknown_value` (tipo "
                "incorrecto), `out_of_range` (intervalo fuera de su regla o versión negativa) "
                "o `field_not_allowed`; campos `hydration.enabled`, "
                "`hydration.interval_minutes`, `nutrition.*`, `expected_version`, o cuerpo "
                "ausente (`field: null`).",
            },
        },
    )
    def put_session_reminder_preferences(
        response: Response,
        body: Annotated[Any, Body()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | ReminderPreferencesResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            preferences = update_preferences(caller, body)
        except _HANDLED as error:
            return _failure(error)
        response.headers["Cache-Control"] = "no-store"
        return ReminderPreferencesResponse.from_preferences(preferences)

    return router
