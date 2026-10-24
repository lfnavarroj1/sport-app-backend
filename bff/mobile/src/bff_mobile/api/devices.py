"""HU037 y HU036 en el canal móvil: dispositivos, mediciones en vivo y umbral (ADR-044).

El BFF autentica (JWKS), exige `athlete` y la capacidad vigente en Afiliación antes de llamar
a Entrenamientos: `devices.measurement.connect` para vincular, listar y revocar dispositivos;
`training.realtime.monitor` para enviar mediciones de una sesión y, desde HU036, para leer y
configurar el umbral (`/v1/me/monitoring-settings`). Después reenvía cuerpo,
`Idempotency-Key` y correlación sin reinterpretarlos: el deportista sale del token y las reglas
(formato del `device_id`, nombre, idempotencia, ventanas de las muestras, sesión activa, estado
del dispositivo) y sus errores los decide `training_management`, que vuelve a verificar todo.
Respuestas, `Location` e `Idempotent-Replayed` llegan sin alterar; la lista y la ingestión
salen con `Cache-Control: no-store`.

Ningún valor de frecuencia cardiaca ni identificador de dispositivo se escribe en los logs.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Body, Header, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from bff_mobile.api.monitoring_settings import (
    AlertSeverity,
    EvaluationStatus,
    add_monitoring_settings_routes,
)
from bff_mobile.clients.training_management import TrainingManagementClient
from bff_mobile.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.errors import ErrorResponse, error_response
from sportapp_platform.tokens import InvalidAccessToken, JwksUnavailable, VerifiedAccessToken

ATHLETE = "athlete"
BEARER_PREFIX = "bearer "
DEVICES_CAPABILITY = "devices.measurement.connect"
REALTIME_CAPABILITY = "training.realtime.monitor"
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
INSTANT = "Instante UTC con sufijo `Z` y precisión de segundos (`YYYY-MM-DDTHH:MM:SSZ`)."
DEVICE_ID = "SHA-256 en 64 caracteres hexadecimales en minúsculas (dirección BLE + usuario)."
# Cabeceras del servicio que el cliente necesita tal como llegan.
RELAYED_HEADERS = ("Cache-Control", "Location", REPLAYED_HEADER, "WWW-Authenticate")

_logger = logging.getLogger("sportapp.bff")

DeviceType = Literal["HEART_RATE_MONITOR"]
DeviceProtocol = Literal["BLE_HRS"]
# Mismas restricciones documentadas que el servicio (ADR-044); el BFF no las aplica.
DeviceId = Annotated[
    str, StringConstraints(pattern=r"^[0-9a-f]{64}$"), Field(description=DEVICE_ID)
]


class RegisterDeviceRequest(BaseModel):
    """Esquema documentado de la vinculación; la validación la hace el servicio (ADR-044)."""

    model_config = ConfigDict(extra="forbid")

    device_id: DeviceId
    display_name: Annotated[
        str,
        StringConstraints(min_length=1, max_length=60),
        Field(description="Nombre visible; se recorta."),
    ]
    device_type: DeviceType
    protocol: DeviceProtocol


class DeviceResponse(BaseModel):
    """Dispositivo vinculado al deportista del token."""

    device_id: DeviceId
    athlete_id: UUID
    display_name: str
    device_type: DeviceType
    protocol: DeviceProtocol
    status: Literal["ACTIVE", "REVOKED"]
    version: Annotated[
        int, Field(description="1 al vincular; +1 en cada revocación o reactivación.")
    ]
    created_at: datetime
    updated_at: datetime
    revoked_at: Annotated[datetime | None, Field(description="`null` mientras está activo.")]


class DevicesResponse(BaseModel):
    """Dispositivos propios (`ACTIVE` y `REVOKED`), `created_at DESC, device_id DESC`."""

    items: list[DeviceResponse]


class MeasurementSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    measured_at: Annotated[datetime, Field(description=INSTANT, examples=["2026-10-24T12:30:05Z"])]
    type: Literal["HEART_RATE"]
    value: Annotated[int, Field(ge=20, le=250, description="Pulsaciones por minuto.")]
    unit: Literal["bpm"]


class MeasurementBatchRequest(BaseModel):
    """Lote de 1 a 60 muestras; una muestra inválida se rechaza sin rechazar el lote."""

    model_config = ConfigDict(extra="forbid")

    device_id: DeviceId
    samples: Annotated[list[MeasurementSample], Field(min_length=1, max_length=60)]


class SampleIssue(StrEnum):
    """Motivos por muestra de `training-management` (ADR-044); el BFF solo los documenta."""

    REQUIRED = "required"
    FIELD_NOT_ALLOWED = "field_not_allowed"
    UNKNOWN_VALUE = "unknown_value"
    INVALID_UNIT = "invalid_unit"
    OUT_OF_RANGE = "out_of_range"
    INVALID_DATETIME = "invalid_datetime"
    TOO_FAR_IN_FUTURE = "too_far_in_future"
    TOO_OLD = "too_old"


class RejectedSample(BaseModel):
    index: Annotated[int, Field(description="Posición de la muestra en `samples`.")]
    issue: Annotated[
        SampleIssue,
        Field(description="Motivo del rechazo de la muestra (catálogo del servicio)."),
    ]


class RiskAlertResponse(BaseModel):
    """HU036: alerta nueva del lote (como máximo una cada 60 s por sesión)."""

    alert_id: Annotated[UUID, Field(description="Estable ante reenvíos del mismo lote.")]
    rule: Literal["HR_ABOVE_THRESHOLD_V1"]
    severity: AlertSeverity
    message: Annotated[str, Field(description="Texto fijo aprobado, con el aviso no diagnóstico.")]
    instruction: Annotated[str, Field(description="Reducir la intensidad o detener la práctica.")]
    detected_at: Annotated[
        datetime, Field(description="Instante UTC de la muestra que completó la ventana de 15 s.")
    ]


class EvaluationResponse(BaseModel):
    """HU036: `NO_DATA` si la última muestra tiene más de 30 s (pérdida de monitoreo); `RISK`
    con 15 s consecutivos sobre el umbral; `OK` en otro caso. No es un diagnóstico médico."""

    status: EvaluationStatus
    alerts: list[RiskAlertResponse]


class MeasurementBatchResponse(BaseModel):
    """Resultado del lote: aceptadas, duplicadas (idempotencia natural), rechazadas y la
    evaluación de riesgo del mismo ciclo (HU036)."""

    session_id: UUID
    device_id: DeviceId
    received: int
    accepted: int
    duplicates: int
    rejected: list[RejectedSample]
    evaluation: EvaluationResponse


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
        extra={"fields": {"upstream": "training-management", "status": status}},
    )
    return _failure(UpstreamUnavailable())


def _passthrough(upstream: httpx.Response, allowed: set[int]) -> Response:
    if upstream.status_code not in allowed:
        return _unexpected(upstream.status_code)
    headers = {name: upstream.headers[name] for name in RELAYED_HEADERS if name in upstream.headers}
    try:
        content = upstream.json()
    except ValueError:
        return _unexpected(upstream.status_code)
    return JSONResponse(status_code=upstream.status_code, content=content, headers=headers)


def _edge(capability: str) -> dict[int | str, dict[str, Any]]:
    return {
        HTTPStatus.UNAUTHORIZED.value: {
            "model": ErrorResponse,
            "description": "`invalid_access_token`: falta el token o no es válido.",
        },
        HTTPStatus.FORBIDDEN.value: {
            "model": ErrorResponse,
            "description": "`permission_required` (no es deportista) o `capability_required` "
            f"(`{capability}`).",
        },
        HTTPStatus.SERVICE_UNAVAILABLE.value: {
            "model": ErrorResponse,
            "description": "`upstream_unavailable`: capacidad no verificable o servicio caído.",
        },
        HTTPStatus.GATEWAY_TIMEOUT.value: {
            "model": ErrorResponse,
            "description": "`upstream_timeout`: capacidad o servicio sin respuesta.",
        },
    }


_REPLAY = {
    REPLAYED_HEADER: {
        "description": "`true` cuando se devuelve el resultado original de la misma clave.",
        "schema": {"type": "string"},
    }
}
_LOCATION = {
    "Location": {"description": "`/v1/me/devices/{device_id}`.", "schema": {"type": "string"}}
}
_NO_STORE = {"Cache-Control": {"description": "`no-store`.", "schema": {"type": "string"}}}
_EDGE_STATUSES = {
    HTTPStatus.UNAUTHORIZED,
    HTTPStatus.FORBIDDEN,
    HTTPStatus.SERVICE_UNAVAILABLE,
    HTTPStatus.GATEWAY_TIMEOUT,
}


def build_devices_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    athlete_snapshot: Callable[[UUID], dict[str, Any]],
    training: TrainingManagementClient,
) -> APIRouter:
    router = APIRouter(tags=["devices"])

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

    def authorized_token(authorization: str | None, capability: str) -> str | Response:
        """Borde: actor y capacidad vigente antes de llamar a Entrenamientos."""
        caller = authenticate_athlete(authorization)
        if isinstance(caller, Response):
            return caller
        user_id, token = caller
        try:
            snapshot = athlete_snapshot(user_id)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _failure(error)
        granted = snapshot.get("capabilities") or []
        if snapshot.get("subscription_status") == "ACTIVE" and capability in granted:
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

    @router.post(
        "/v1/me/devices",
        status_code=HTTPStatus.CREATED,
        response_model=DeviceResponse,
        summary="Vincular o reactivar un dispositivo de medición (HU037)",
        openapi_extra=_request_body(RegisterDeviceRequest),
        responses={
            **_edge(DEVICES_CAPABILITY),
            HTTPStatus.CREATED.value: {"headers": {**_LOCATION, **_REPLAY}},
            HTTPStatus.OK.value: {
                "model": DeviceResponse,
                "description": "El mismo `device_id` ya activo con el mismo nombre y otra "
                "clave: sin cambios.",
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`device_already_registered` (activo con otro nombre) o "
                "`idempotency_key_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` con `details[{field, issue}]`: `required`, "
                "`field_not_allowed`, `unknown_value`, `invalid_format` (`device_id`), "
                "`invalid_length` (`display_name`); `Idempotency-Key` `required` o "
                "`invalid_uuid`.",
            },
        },
    )
    def register_device(
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = authorized_token(authorization, DEVICES_CAPABILITY)
        if isinstance(token, Response):
            return token
        return relay(
            lambda: training.register_device(token, body, idempotency_key),
            {
                HTTPStatus.CREATED,
                HTTPStatus.OK,
                HTTPStatus.CONFLICT,
                HTTPStatus.UNPROCESSABLE_ENTITY,
                *_EDGE_STATUSES,
            },
        )

    @router.get(
        "/v1/me/devices",
        response_model=DevicesResponse,
        summary="Mis dispositivos de medición (HU037)",
        responses={**_edge(DEVICES_CAPABILITY), HTTPStatus.OK.value: {"headers": _NO_STORE}},
    )
    def list_devices(authorization: Annotated[str | None, Header()] = None) -> Response:
        token = authorized_token(authorization, DEVICES_CAPABILITY)
        if isinstance(token, Response):
            return token
        response = relay(lambda: training.list_devices(token), {HTTPStatus.OK, *_EDGE_STATUSES})
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.delete(
        "/v1/me/devices/{device_id}",
        response_model=DeviceResponse,
        summary="Revocar un dispositivo de medición propio (HU037)",
        responses={
            **_edge(DEVICES_CAPABILITY),
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": "`device_not_found`: inexistente o de otro deportista.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `device_id` `invalid_format`.",
            },
        },
    )
    def revoke_device(
        device_id: str, authorization: Annotated[str | None, Header()] = None
    ) -> Response:
        token = authorized_token(authorization, DEVICES_CAPABILITY)
        if isinstance(token, Response):
            return token
        return relay(
            lambda: training.revoke_device(token, device_id),
            {HTTPStatus.OK, HTTPStatus.NOT_FOUND, HTTPStatus.UNPROCESSABLE_ENTITY, *_EDGE_STATUSES},
        )

    @router.post(
        "/v1/training-sessions/{session_id}/measurements",
        response_model=MeasurementBatchResponse,
        summary="Enviar mediciones en vivo y recibir su evaluación de riesgo (HU037, HU036)",
        openapi_extra=_request_body(MeasurementBatchRequest),
        responses={
            **_edge(REALTIME_CAPABILITY),
            HTTPStatus.OK.value: {"headers": _NO_STORE},
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": "`training_session_not_found` (inexistente o ajena) o "
                "`device_not_found` (no vinculado al deportista).",
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`training_session_already_finished` o `device_revoked`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` del lote: `session_id` `invalid_uuid`, "
                "`device_id` `invalid_format`, `samples` `required`, `unknown_value` o "
                "`too_many_items`, `field_not_allowed` o cuerpo ausente.",
            },
        },
    )
    def ingest_measurements(
        session_id: str,
        body: Annotated[Any, Body()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = authorized_token(authorization, REALTIME_CAPABILITY)
        if isinstance(token, Response):
            return token
        response = relay(
            lambda: training.ingest_measurements(token, session_id, body),
            {
                HTTPStatus.OK,
                HTTPStatus.NOT_FOUND,
                HTTPStatus.CONFLICT,
                HTTPStatus.UNPROCESSABLE_ENTITY,
                *_EDGE_STATUSES,
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    add_monitoring_settings_routes(
        router,
        authorized_token=lambda authorization: authorized_token(authorization, REALTIME_CAPABILITY),
        relay=relay,
        training=training,
        responses=_edge(REALTIME_CAPABILITY),
        edge_statuses=_EDGE_STATUSES,
        request_body=_request_body,
    )
    return router
