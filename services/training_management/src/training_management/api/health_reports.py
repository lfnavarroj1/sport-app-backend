"""HU042: reportar una molestia, incapacidad o lesión (contrato de ADR-037).

`POST /v1/me/health-reports` registra el reporte (`201` con `status: OPEN` y `version: 1`).
La misma `Idempotency-Key` devuelve la respuesta original (`201` + `Idempotent-Replayed:
true`) y el mismo `report_id` con los mismos datos y otra clave devuelve el reporte existente
(`201`), sin otro evento.

Solo `athlete`, sin capacidad de plan (opción base «Mi salud»); el deportista sale del token.
El cuerpo se lee como JSON sin validación automática para responder `details[{field, issue}]`
con todas las reglas incumplidas; su esquema se documenta en el OpenAPI. Instantes en UTC con
sufijo `Z`. La respuesta nunca incluye diagnósticos ni recomendaciones.
"""

from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Header, Response
from pydantic import BaseModel, ConfigDict, Field

from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from training_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    correlation_uuid,
    forbidden,
)
from training_management.application.commands.report_health import (
    IDEMPOTENCY_HEADER,
    ReportHealth,
    ReportHealthCommand,
    ReportOutcome,
)
from training_management.application.errors import (
    IdempotencyKeyReused,
    PermissionRequired,
    ReportIdReused,
    TrainingSessionNotFound,
)
from training_management.application.health_documents import health_report_document
from training_management.domain.health.model import (
    RECOVERY_DAYS_MAX,
    RECOVERY_DAYS_MIN,
    BodyArea,
    ReportType,
    Severity,
)
from training_management.domain.health.validation import InvalidHealthReport

REPLAYED_HEADER = "Idempotent-Replayed"
INSTANT = "Instante UTC con sufijo `Z` y precisión de segundos (`YYYY-MM-DDTHH:MM:SSZ`)."

_ERRORS: dict[type[Exception], tuple[HTTPStatus, str]] = {
    IdempotencyKeyReused: (
        HTTPStatus.CONFLICT,
        "La clave de idempotencia ya se usó con otra solicitud.",
    ),
    ReportIdReused: (
        HTTPStatus.CONFLICT,
        "El identificador del reporte ya se usó con otros datos.",
    ),
    TrainingSessionNotFound: (
        HTTPStatus.NOT_FOUND,
        "La sesión de entrenamiento no existe.",
    ),
}

_HANDLED: tuple[type[Exception], ...] = (PermissionRequired, InvalidHealthReport, *_ERRORS)


class HealthConsent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Annotated[
        str, Field(description="Igual a la versión vigente (`HEALTH_DATA_CONSENT_VERSION`).")
    ]


class ReportHealthRequest(BaseModel):
    """Esquema documentado del reporte; la validación la hace el dominio (ADR-037)."""

    model_config = ConfigDict(extra="forbid")

    report_id: Annotated[UUID, Field(description="UUID v4 generado por el móvil.")]
    type: ReportType
    body_area: BodyArea
    severity: Severity
    occurred_at: Annotated[
        datetime,
        Field(
            description=f"{INSTANT} Hasta 5 minutos en el futuro y 365 días en el pasado.",
            examples=["2026-10-24T18:30:00Z"],
        ),
    ]
    session_id: Annotated[
        UUID | None,
        Field(description="Sesión propia de entrenamiento (HU032); opcional."),
    ] = None
    expected_recovery_days: Annotated[
        int | None,
        Field(ge=RECOVERY_DAYS_MIN, le=RECOVERY_DAYS_MAX, description="Opcional."),
    ] = None
    consent: HealthConsent


class HealthReportResponse(BaseModel):
    """Reporte confirmado; nace `OPEN` y no incluye diagnóstico ni recomendación."""

    report_id: UUID
    athlete_id: UUID
    type: ReportType
    body_area: BodyArea
    severity: Severity
    occurred_at: datetime
    session_id: UUID | None
    expected_recovery_days: int | None
    consent: HealthConsent
    status: Literal["OPEN"]
    reported_at: datetime
    version: int


def _request_body() -> dict[str, Any]:
    schema = ReportHealthRequest.model_json_schema(ref_template="#/components/schemas/{model}")
    schema.pop("$defs", None)
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": schema,
                    "example": {
                        "report_id": "6f1f7f9e-2a4b-4c55-9a51-2f7c0b7f3c11",
                        "type": "DISCOMFORT",
                        "body_area": "KNEE",
                        "severity": "MODERATE",
                        "occurred_at": "2026-10-24T18:30:00Z",
                        "session_id": None,
                        "expected_recovery_days": 3,
                        "consent": {"version": "local-dev"},
                    },
                }
            },
        }
    }


def _failure(error: Exception) -> Response:
    if isinstance(error, PermissionRequired):
        return forbidden(error)
    if isinstance(error, InvalidHealthReport):
        return error_response(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            error.code,
            "La solicitud no es válida.",
            [ErrorDetail(field=item.field, issue=item.issue.value) for item in error.violations],
        )
    status, message = _ERRORS[type(error)]
    return error_response(status, getattr(error, "code", ""), message)


def build_health_reports_router(
    *, verify_token: TokenVerifier, report_health: ReportHealth
) -> APIRouter:
    router = APIRouter(prefix="/v1/me/health-reports", tags=["health-reports"])

    @router.post(
        "",
        status_code=HTTPStatus.CREATED,
        response_model=HealthReportResponse,
        summary="Reportar una molestia, incapacidad o lesión (HU042)",
        openapi_extra=_request_body(),
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.CREATED.value: {
                "description": "Reporte `OPEN` versión 1. También el reintento con la misma "
                "clave (`Idempotent-Replayed: true`) y el mismo `report_id` con los mismos "
                "datos (reporte existente, sin otro evento).",
                "headers": {
                    REPLAYED_HEADER: {
                        "description": "`true` cuando se devuelve la respuesta original de la "
                        "misma clave.",
                        "schema": {"type": "string"},
                    }
                },
            },
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required`: solo un deportista puede reportar.",
            },
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": "`training_session_not_found`: `session_id` no existe o no es "
                "una sesión propia; no se persiste nada.",
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`idempotency_key_reused` o `report_id_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` con `details[{field, issue}]`: `required`, "
                "`field_not_allowed` (incluidos `athlete_id`, `status` y `version`), "
                "`unknown_value` (fuera de catálogo o tipo JSON inválido), `invalid_uuid` "
                "(`report_id`, `session_id`, `Idempotency-Key`), `invalid_datetime`, "
                "`too_far_in_future`, `too_old`, `out_of_range` "
                "(`expected_recovery_days`) y `unsupported_consent_version`.",
            },
        },
    )
    def report(
        response: Response,
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | HealthReportResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            result = report_health(
                caller,
                ReportHealthCommand(
                    body=body, idempotency_key=idempotency_key, correlation_id=correlation_uuid()
                ),
            )
        except _HANDLED as error:
            return _failure(error)
        if result.outcome is ReportOutcome.REPLAYED:
            response.headers[REPLAYED_HEADER] = "true"
        response.headers["Cache-Control"] = "no-store"
        return HealthReportResponse.model_validate(health_report_document(result.report))

    return router
