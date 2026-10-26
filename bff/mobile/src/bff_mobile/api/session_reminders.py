"""HU038 en el canal móvil: reglas y preferencias de recordatorios durante la sesión (ADR-045).

`GET /v1/session-reminder-rules` y `GET`/`PUT /v1/me/session-reminder-preferences`. El BFF
autentica (JWKS), exige `athlete` y la capacidad vigente `nutrition.session.reminders`
(Afiliación) antes de llamar a Entrenamientos; después reenvía el cuerpo tal cual y entrega la
respuesta sin alterarla. El ruleset, los rangos, `expected_version` y sus errores los decide
`training_management`, que vuelve a verificar actor y capacidad. El BFF no evalúa ni envía
recordatorios: lo hace el dispositivo. Ningún valor de las preferencias se escribe en los logs.
"""

from collections.abc import Callable
from enum import StrEnum
from http import HTTPStatus
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Body, Header, Response
from pydantic import BaseModel, ConfigDict, Field

from bff_mobile.api.devices import (
    _EDGE_STATUSES,
    _NO_STORE,
    ATHLETE,
    BEARER_PREFIX,
    _edge,
    _failure,
    _passthrough,
    _request_body,
    _unauthenticated,
)
from bff_mobile.clients.training_management import TrainingManagementClient
from bff_mobile.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.errors import ErrorResponse, error_response
from sportapp_platform.tokens import InvalidAccessToken, JwksUnavailable, VerifiedAccessToken

SESSION_REMINDERS_CAPABILITY = "nutrition.session.reminders"
RULESET_VERSION = "session-reminders-2026-10-13.v1"
# Rangos del ruleset vigente, solo para documentar el contrato; los aplica el servicio.
HYDRATION_MIN, HYDRATION_MAX = 10, 60
NUTRITION_MIN, NUTRITION_MAX = 30, 120


class ReminderType(StrEnum):
    HYDRATION = "HYDRATION"
    NUTRITION = "NUTRITION"


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


class ReminderRulesResponse(BaseModel):
    """Ruleset versionado e inmutable por versión; el dispositivo lo evalúa sin red."""

    ruleset_version: Annotated[str, Field(examples=[RULESET_VERSION])]
    rules: list[ReminderRuleResponse]


class HydrationSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    interval_minutes: Annotated[
        int,
        Field(
            ge=HYDRATION_MIN,
            le=HYDRATION_MAX,
            description=f"Minutos entre recordatorios ({HYDRATION_MIN}-{HYDRATION_MAX}).",
        ),
    ]


class NutritionSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    interval_minutes: Annotated[
        int,
        Field(
            ge=NUTRITION_MIN,
            le=NUTRITION_MAX,
            description=f"Minutos entre recordatorios ({NUTRITION_MIN}-{NUTRITION_MAX}).",
        ),
    ]


class ReminderPreferencesRequest(BaseModel):
    """Esquema documentado del `PUT`; la validación la hace el servicio."""

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


def build_session_reminders_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    athlete_snapshot: Callable[[UUID], dict[str, Any]],
    training: TrainingManagementClient,
) -> APIRouter:
    router = APIRouter(tags=["session-reminders"])

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
            return _failure(UpstreamUnavailable())
        if identity.actor_type != ATHLETE:
            return error_response(
                HTTPStatus.FORBIDDEN,
                "permission_required",
                "Su tipo de usuario no puede realizar esta operación.",
            )
        return user_id, token

    def authorized_token(authorization: str | None) -> str | Response:
        """Borde: token, actor `athlete` y capacidad antes de llamar a Entrenamientos."""
        caller = authenticate_athlete(authorization)
        if isinstance(caller, Response):
            return caller
        user_id, token = caller
        try:
            snapshot = athlete_snapshot(user_id)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _failure(error)
        granted = snapshot.get("capabilities") or []
        if (
            snapshot.get("subscription_status") == "ACTIVE"
            and SESSION_REMINDERS_CAPABILITY in granted
        ):
            return token
        return error_response(
            HTTPStatus.FORBIDDEN, "capability_required", "Su plan no incluye esta funcionalidad."
        )

    def relay(send: Callable[[], httpx.Response], allowed: set[int]) -> Response:
        try:
            upstream = send()
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _failure(error)
        return _passthrough(upstream, allowed)

    edge = _edge(SESSION_REMINDERS_CAPABILITY)

    @router.get(
        "/v1/session-reminder-rules",
        response_model=ReminderRulesResponse,
        summary="Reglas versionadas de recordatorios durante la sesión (HU038)",
        description="Ruleset `session-reminders-2026-10-13.v1`: `HYDRATION` y `NUTRITION` con "
        "intervalo por defecto, rango, `applies_after_minutes`, mensaje aprobado y aviso. La "
        "app lo guarda y lo evalúa sin red sobre el cronómetro de la sesión activa.",
        responses=edge,
    )
    def get_session_reminder_rules(
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = authorized_token(authorization)
        if isinstance(token, Response):
            return token
        return relay(
            lambda: training.session_reminder_rules(token), {HTTPStatus.OK, *_EDGE_STATUSES}
        )

    @router.get(
        "/v1/me/session-reminder-preferences",
        response_model=ReminderPreferencesResponse,
        summary="Mis preferencias de recordatorios durante la sesión (HU038)",
        description="Sin preferencias guardadas: valores por defecto del ruleset, ambos tipos "
        "habilitados y `version: 0`.",
        responses={**edge, HTTPStatus.OK.value: {"headers": _NO_STORE}},
    )
    def get_session_reminder_preferences(
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = authorized_token(authorization)
        if isinstance(token, Response):
            return token
        response = relay(
            lambda: training.session_reminder_preferences(token),
            {HTTPStatus.OK, *_EDGE_STATUSES},
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.put(
        "/v1/me/session-reminder-preferences",
        response_model=ReminderPreferencesResponse,
        summary="Configurar mis recordatorios durante la sesión (HU038)",
        description="Reemplaza las preferencias con `expected_version` (la versión leída). "
        "Repetir los mismos valores no cambia la versión.",
        openapi_extra=_request_body(ReminderPreferencesRequest),
        responses={
            **edge,
            HTTPStatus.OK.value: {"headers": _NO_STORE},
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`version_conflict`: `details[0]` = `{field: expected_version, "
                "issue: <versión vigente>}`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `required`, `unknown_value`, "
                "`out_of_range` o `field_not_allowed` en `hydration.*`, `nutrition.*` o "
                "`expected_version`; cuerpo ausente con `field: null`.",
            },
        },
    )
    def put_session_reminder_preferences(
        body: Annotated[Any, Body()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = authorized_token(authorization)
        if isinstance(token, Response):
            return token
        response = relay(
            lambda: training.update_session_reminder_preferences(token, body),
            {HTTPStatus.OK, HTTPStatus.CONFLICT, HTTPStatus.UNPROCESSABLE_ENTITY, *_EDGE_STATUSES},
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    return router
