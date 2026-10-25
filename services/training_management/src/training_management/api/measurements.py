"""HU037/HU036 (ADR-044 §5-§9): `POST /v1/training-sessions/{session_id}/measurements`.

El móvil envía cada 5 s las muestras de 1 Hz de un monitor vinculado durante una sesión propia
`ACTIVE`. Cada muestra se valida por separado: las inválidas vuelven en `rejected[]` sin
rechazar el lote y las repetidas se cuentan en `duplicates` (idempotencia natural, sin
`Idempotency-Key`). Solo `athlete` con `training.realtime.monitor`. El cuerpo se lee como JSON
sin validación automática; su esquema se documenta en el OpenAPI. HU036: la misma respuesta
trae la evaluación `HR_ABOVE_THRESHOLD_V1` (`OK`, `RISK` o `NO_DATA`) con las alertas nuevas.
"""

from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Header, Response
from pydantic import BaseModel, ConfigDict, Field

from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from training_management.api.devices import DeviceId
from training_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    correlation_uuid,
    forbidden,
)
from training_management.application.access import REALTIME_MONITOR_CAPABILITY
from training_management.application.commands.measurements import (
    IngestionResult,
    IngestMeasurements,
    IngestMeasurementsCommand,
)
from training_management.application.errors import (
    AthleteContextUnavailable,
    CapabilitiesTimeout,
    CapabilitiesUnavailable,
    CapabilityRequired,
    PermissionRequired,
    TrainingSessionAlreadyFinished,
    TrainingSessionNotFound,
)
from training_management.domain.devices.model import DeviceNotFound, DeviceRevoked
from training_management.domain.measurements.model import (
    MAX_HEART_RATE,
    MAX_SAMPLES,
    MIN_HEART_RATE,
    InvalidMeasurementBatch,
    SampleIssue,
)
from training_management.domain.monitoring.model import (
    ALERT_INSTRUCTION,
    ALERT_MESSAGE,
    RULE,
    AlertSeverity,
    Evaluation,
    EvaluationStatus,
)


class MeasurementSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    measured_at: Annotated[
        datetime,
        Field(
            description="Instante UTC `YYYY-MM-DDTHH:MM:SSZ` entre el inicio de la sesión y "
            "ahora + 5 s, con antigüedad de 15 minutos como máximo.",
            examples=["2026-10-12T12:30:05Z"],
        ),
    ]
    type: Literal["HEART_RATE"]
    value: Annotated[int, Field(ge=MIN_HEART_RATE, le=MAX_HEART_RATE)]
    unit: Literal["bpm"]


class MeasurementBatchRequest(BaseModel):
    """Esquema documentado del lote; la validación la hacen el transporte y el dominio."""

    model_config = ConfigDict(extra="forbid")

    device_id: DeviceId
    samples: Annotated[list[MeasurementSample], Field(min_length=1, max_length=MAX_SAMPLES)]


class RejectedSample(BaseModel):
    index: Annotated[int, Field(description="Posición de la muestra en `samples` (desde 0).")]
    issue: Annotated[SampleIssue, Field(description="Motivo del rechazo de la muestra.")]


class RiskAlertResponse(BaseModel):
    """Alerta nueva de este lote (como máximo una cada 60 s por sesión)."""

    alert_id: Annotated[UUID, Field(description="Estable ante reenvíos del mismo lote.")]
    rule: Annotated[Literal["HR_ABOVE_THRESHOLD_V1"], Field(description="Regla aplicada.")]
    severity: AlertSeverity
    message: Annotated[str, Field(description="Texto fijo aprobado, con el aviso no diagnóstico.")]
    instruction: Annotated[str, Field(description="Qué hacer: reducir la intensidad o detenerse.")]
    detected_at: Annotated[
        datetime, Field(description="Instante UTC de la muestra que completó la ventana.")
    ]


class EvaluationResponse(BaseModel):
    """`NO_DATA`: la muestra más reciente del dispositivo tiene más de 30 s (pérdida de
    monitoreo; no se evalúa). `RISK`: ventana de 15 s sobre el umbral. `OK` en otro caso."""

    status: EvaluationStatus
    alerts: list[RiskAlertResponse]

    @classmethod
    def from_evaluation(cls, evaluation: Evaluation) -> "EvaluationResponse":
        return cls(
            status=evaluation.status,
            alerts=[
                RiskAlertResponse(
                    alert_id=alert.alert_id,
                    rule=RULE,
                    severity=alert.severity,
                    message=ALERT_MESSAGE,
                    instruction=ALERT_INSTRUCTION,
                    detected_at=alert.detected_at,
                )
                for alert in evaluation.alerts
            ],
        )


class MeasurementBatchResponse(BaseModel):
    """Resultado del lote: `received = accepted + duplicates + len(rejected)`."""

    session_id: UUID
    device_id: DeviceId
    received: int
    accepted: int
    duplicates: Annotated[
        int, Field(description="Muestras válidas ya registradas (en este u otro lote).")
    ]
    rejected: list[RejectedSample]
    evaluation: Annotated[
        EvaluationResponse,
        Field(description="HU036: evaluación de riesgo dentro del mismo ciclo (ADR-044 §8)."),
    ]

    @classmethod
    def from_result(cls, result: IngestionResult) -> "MeasurementBatchResponse":
        return cls(
            session_id=result.session_id,
            device_id=result.device_id,
            received=result.received,
            accepted=result.accepted,
            duplicates=result.duplicates,
            rejected=[
                RejectedSample(index=item.index, issue=item.issue) for item in result.rejected
            ],
            evaluation=EvaluationResponse.from_evaluation(result.evaluation),
        )


def _inline(node: Any, definitions: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_inline(item, definitions) for item in node]
    if not isinstance(node, dict):
        return node
    reference = node.get("$ref")
    if isinstance(reference, str):
        return _inline(definitions[reference.rsplit("/", 1)[-1]], definitions)
    return {key: _inline(value, definitions) for key, value in node.items()}


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    """Cuerpo documentado con la muestra en línea (no es un componente de ninguna respuesta)."""
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _inline(schema, definitions)}},
        }
    }


_MESSAGES: dict[type[Exception], tuple[HTTPStatus, str]] = {
    TrainingSessionNotFound: (HTTPStatus.NOT_FOUND, "La sesión de entrenamiento no existe."),
    DeviceNotFound: (HTTPStatus.NOT_FOUND, "El dispositivo no está vinculado."),
    TrainingSessionAlreadyFinished: (
        HTTPStatus.CONFLICT,
        "La sesión de entrenamiento ya terminó.",
    ),
    DeviceRevoked: (HTTPStatus.CONFLICT, "El dispositivo fue revocado."),
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
    InvalidMeasurementBatch,
    *_MESSAGES,
)


def _failure(error: Exception) -> Response:
    if isinstance(error, PermissionRequired | CapabilityRequired):
        return forbidden(error)
    if isinstance(error, InvalidMeasurementBatch):
        return error_response(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            error.code,
            "La solicitud no es válida.",
            [ErrorDetail(field=field, issue=issue) for field, issue in error.violations],
        )
    status, message = _MESSAGES[type(error)]
    return error_response(status, getattr(error, "code", ""), message)


def build_measurements_router(
    *, verify_token: TokenVerifier, ingest: IngestMeasurements
) -> APIRouter:
    router = APIRouter(prefix="/v1/training-sessions", tags=["training-sessions"])

    @router.post(
        "/{session_id}/measurements",
        response_model=MeasurementBatchResponse,
        summary="Enviar y evaluar un lote de frecuencia cardiaca de una sesión activa "
        "(HU037, HU036)",
        description="Lote de 1 a 60 muestras de un dispositivo `ACTIVE` del deportista. Las "
        "muestras se identifican por (`session_id`, `device_id`, `measured_at`): reenviar un "
        "lote es seguro, solo suma `duplicates` y no repite alertas. Fuera de orden se aceptan. "
        "`evaluation` aplica `HR_ABOVE_THRESHOLD_V1` (valor > umbral durante ≥ 15 s "
        "consecutivos, alerta como máximo cada 60 s) con el umbral de "
        "`/v1/me/monitoring-settings`; `NO_DATA` si la última muestra tiene más de 30 s. No es "
        "un diagnóstico médico.",
        openapi_extra=_schema(MeasurementBatchRequest),
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required` (no es deportista) o `capability_required` "
                f"(`{REALTIME_MONITOR_CAPABILITY}`).",
            },
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": "`training_session_not_found` (inexistente o ajena) o "
                "`device_not_found` (no vinculado al deportista).",
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`training_session_already_finished` (la sesión no está "
                "`ACTIVE`) o `device_revoked`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` del lote: `session_id` `invalid_uuid`, "
                "`device_id` `required`/`unknown_value`/`invalid_format`, `samples` "
                "`required`/`unknown_value`/`too_many_items`, `field_not_allowed` o cuerpo "
                "ausente. Las muestras inválidas no producen `422`: van en `rejected[]`.",
            },
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": "`upstream_unavailable`: no se pudo verificar el plan o, sin "
                "umbral propio, obtener la fecha de nacimiento para el umbral por defecto.",
            },
            HTTPStatus.GATEWAY_TIMEOUT.value: {
                "model": ErrorResponse,
                "description": "`upstream_timeout`: Afiliación no respondió a tiempo.",
            },
        },
    )
    def ingest_measurements(
        session_id: str,
        response: Response,
        body: Annotated[Any, Body()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | MeasurementBatchResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            result = ingest(
                caller,
                IngestMeasurementsCommand(
                    session_id=session_id, body=body, correlation_id=correlation_uuid()
                ),
            )
        except _HANDLED as error:
            return _failure(error)
        response.headers["Cache-Control"] = "no-store"
        return MeasurementBatchResponse.from_result(result)

    return router
