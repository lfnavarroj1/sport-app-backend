"""HU036 en el canal móvil: umbral de alerta de frecuencia cardiaca (ADR-044 §9).

`GET`/`PUT /v1/me/monitoring-settings` con el mismo filtro de borde que la ingestión de
mediciones (`athlete` con `training.realtime.monitor`, ver `api.devices`): el BFF no valida el
umbral ni calcula el valor por defecto; reenvía el cuerpo y entrega la respuesta de
`training_management` sin alterarla, con `Cache-Control: no-store`.
"""

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Body, Header, Response
from pydantic import BaseModel, ConfigDict, Field

from bff_mobile.clients.training_management import TrainingManagementClient
from sportapp_platform.errors import ErrorResponse

THRESHOLD_MIN = 100
THRESHOLD_MAX = 220


class EvaluationStatus(StrEnum):
    OK = "OK"
    RISK = "RISK"
    NO_DATA = "NO_DATA"


class AlertSeverity(StrEnum):
    HIGH = "HIGH"


class ThresholdSource(StrEnum):
    CUSTOM = "CUSTOM"
    AGE_DEFAULT = "AGE_DEFAULT"
    FIXED_DEFAULT = "FIXED_DEFAULT"


class MonitoringSettingsRequest(BaseModel):
    """Esquema documentado del `PUT`; la validación la hace el servicio."""

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
        Field(description="Última configuración (UTC); `null` con un valor por defecto."),
    ]
    disclaimer: Annotated[str, Field(description="Aviso fijo: el umbral no es clínico (R-21).")]


_NO_STORE = {"Cache-Control": {"description": "`no-store`.", "schema": {"type": "string"}}}


def add_monitoring_settings_routes(
    router: APIRouter,
    *,
    authorized_token: Callable[[str | None], str | Response],
    relay: Callable[[Callable[[], httpx.Response], set[int]], Response],
    training: TrainingManagementClient,
    responses: dict[int | str, dict[str, Any]],
    edge_statuses: set[HTTPStatus],
    request_body: Callable[[type[BaseModel]], dict[str, Any]],
) -> None:
    @router.get(
        "/v1/me/monitoring-settings",
        response_model=MonitoringSettingsResponse,
        summary="Umbral de alerta de frecuencia cardiaca vigente (HU036)",
        responses={**responses, HTTPStatus.OK.value: {"headers": _NO_STORE}},
    )
    def get_monitoring_settings(authorization: Annotated[str | None, Header()] = None) -> Response:
        token = authorized_token(authorization)
        if isinstance(token, Response):
            return token
        response = relay(
            lambda: training.monitoring_settings(token), {HTTPStatus.OK, *edge_statuses}
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.put(
        "/v1/me/monitoring-settings",
        response_model=MonitoringSettingsResponse,
        summary="Configurar el umbral de alerta de frecuencia cardiaca (HU036)",
        description="Reemplaza el umbral propio (100-220 lpm); repetir el mismo valor no cambia "
        "nada. No es un umbral clínico.",
        openapi_extra=request_body(MonitoringSettingsRequest),
        responses={
            **responses,
            HTTPStatus.OK.value: {"headers": _NO_STORE},
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `max_heart_rate_alert_bpm` `required`, "
                "`unknown_value` u `out_of_range`; `field_not_allowed` o cuerpo ausente.",
            },
        },
    )
    def put_monitoring_settings(
        body: Annotated[Any, Body()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = authorized_token(authorization)
        if isinstance(token, Response):
            return token
        response = relay(
            lambda: training.update_monitoring_settings(token, body),
            {HTTPStatus.OK, HTTPStatus.UNPROCESSABLE_ENTITY, *edge_statuses},
        )
        response.headers["Cache-Control"] = "no-store"
        return response
