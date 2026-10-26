"""HU036 (ADR-044 §9): `GET`/`PUT /v1/me/monitoring-settings`.

Umbral de alerta de frecuencia cardiaca del deportista del token (`athlete` con
`training.realtime.monitor`). `PUT` reemplaza el umbral (100-220 lpm) y es idempotente: repetir
el mismo valor no cambia nada. Sin configuración, `GET` devuelve el valor por defecto
(`round(0.9 x (220 - edad))` o `171`) y su origen. Ambas respuestas llevan el aviso «no es un
diagnóstico médico» (R-21). El cuerpo se lee como JSON sin validación automática para responder
`details[{field, issue}]`; su esquema se documenta en el OpenAPI. `Cache-Control: no-store`.
"""

from datetime import datetime
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
from training_management.application.access import REALTIME_MONITOR_CAPABILITY
from training_management.application.commands.monitoring_settings import (
    UpdateMonitoringSettings,
)
from training_management.application.errors import (
    AthleteContextUnavailable,
    CapabilitiesTimeout,
    CapabilitiesUnavailable,
    CapabilityRequired,
    PermissionRequired,
)
from training_management.application.queries.monitoring_settings import GetMonitoringSettings
from training_management.domain.monitoring.model import (
    DISCLAIMER,
    THRESHOLD_MAX,
    THRESHOLD_MIN,
    InvalidMonitoringSettings,
    Threshold,
    ThresholdSource,
)


class MonitoringSettingsRequest(BaseModel):
    """Esquema documentado del `PUT`; la validación la hace el dominio."""

    model_config = ConfigDict(extra="forbid")

    max_heart_rate_alert_bpm: Annotated[
        int,
        Field(
            ge=THRESHOLD_MIN,
            le=THRESHOLD_MAX,
            description="Umbral de alerta en lpm; la alerta salta con valores mayores.",
        ),
    ]


class MonitoringSettingsResponse(BaseModel):
    max_heart_rate_alert_bpm: Annotated[
        int, Field(ge=THRESHOLD_MIN, le=THRESHOLD_MAX, description="Umbral vigente en lpm.")
    ]
    source: Annotated[
        ThresholdSource,
        Field(
            description="`CUSTOM` (configurado), `AGE_DEFAULT` (`round(0.9 x (220 - edad))`) "
            "o `FIXED_DEFAULT` (`171`, sin fecha de nacimiento)."
        ),
    ]
    updated_at: Annotated[
        datetime | None,
        Field(
            description="Última configuración (UTC); `null` mientras se usa un valor por defecto."
        ),
    ]
    disclaimer: Annotated[str, Field(description="Aviso fijo: el umbral no es clínico (R-21).")]

    @classmethod
    def from_threshold(cls, threshold: Threshold) -> "MonitoringSettingsResponse":
        settings = threshold.settings
        return cls(
            max_heart_rate_alert_bpm=threshold.value,
            source=threshold.source,
            updated_at=settings.updated_at if settings is not None else None,
            disclaimer=DISCLAIMER,
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
    AthleteContextUnavailable: (
        HTTPStatus.SERVICE_UNAVAILABLE,
        "No fue posible determinar su umbral de alerta. Intente de nuevo.",
    ),
}
_HANDLED: tuple[type[Exception], ...] = (
    PermissionRequired,
    CapabilityRequired,
    InvalidMonitoringSettings,
    *_MESSAGES,
)
_EDGE: dict[int | str, dict[str, Any]] = {
    **AUTH_RESPONSES,
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required` (no es deportista) o `capability_required` "
        f"(`{REALTIME_MONITOR_CAPABILITY}`).",
    },
    HTTPStatus.GATEWAY_TIMEOUT.value: {
        "model": ErrorResponse,
        "description": "`upstream_timeout`: Afiliación no respondió a tiempo.",
    },
}


def _failure(error: Exception) -> Response:
    if isinstance(error, PermissionRequired | CapabilityRequired):
        return forbidden(error)
    if isinstance(error, InvalidMonitoringSettings):
        return error_response(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            error.code,
            "La solicitud no es válida.",
            [ErrorDetail(field=field, issue=issue) for field, issue in error.violations],
        )
    status, message = _MESSAGES[type(error)]
    return error_response(status, getattr(error, "code", ""), message)


def _body_schema() -> dict[str, Any]:
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": MonitoringSettingsRequest.model_json_schema()}
            },
        }
    }


def build_monitoring_settings_router(
    *,
    verify_token: TokenVerifier,
    get_settings: GetMonitoringSettings,
    update_settings: UpdateMonitoringSettings,
) -> APIRouter:
    router = APIRouter(prefix="/v1/me/monitoring-settings", tags=["training-monitoring"])

    @router.get(
        "",
        response_model=MonitoringSettingsResponse,
        summary="Umbral de alerta de frecuencia cardiaca vigente (HU036)",
        responses={
            **_EDGE,
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": "`upstream_unavailable`: no se pudo verificar el plan o, sin "
                "umbral propio, obtener la fecha de nacimiento de Usuarios.",
            },
        },
    )
    def get_monitoring_settings(
        response: Response, authorization: Annotated[str | None, Header()] = None
    ) -> Response | MonitoringSettingsResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            threshold = get_settings(caller)
        except _HANDLED as error:
            return _failure(error)
        response.headers["Cache-Control"] = "no-store"
        return MonitoringSettingsResponse.from_threshold(threshold)

    @router.put(
        "",
        response_model=MonitoringSettingsResponse,
        summary="Configurar el umbral de alerta de frecuencia cardiaca (HU036)",
        description="Reemplaza el umbral propio (100-220 lpm). Repetir el mismo valor no "
        "cambia nada. No es un umbral clínico.",
        openapi_extra=_body_schema(),
        responses={
            **_EDGE,
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `max_heart_rate_alert_bpm` `required`, "
                "`unknown_value` (no entero) u `out_of_range` (fuera de 100-220); "
                "`field_not_allowed` o cuerpo ausente.",
            },
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": "`upstream_unavailable`: no se pudo verificar el plan.",
            },
        },
    )
    def put_monitoring_settings(
        response: Response,
        body: Annotated[Any, Body()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | MonitoringSettingsResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            threshold = update_settings(caller, body)
        except _HANDLED as error:
            return _failure(error)
        response.headers["Cache-Control"] = "no-store"
        return MonitoringSettingsResponse.from_threshold(threshold)

    return router
