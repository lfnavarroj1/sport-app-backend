"""HU042 en el canal móvil: reportar una molestia, incapacidad o lesión (contrato de ADR-037).

El BFF autentica (JWKS) y aplica el filtro de borde por actor (`athlete`); «Mi salud» es una
opción base, sin capacidad de plan. Después reenvía cuerpo, `Idempotency-Key` y correlación
sin reinterpretarlos: el deportista sale del token y el BFF no acepta `athlete_id` (el
servicio lo rechaza con `field_not_allowed`). Catálogos, rangos, consentimiento, sesión
propia, idempotencia y sus errores los decide `training_management`, que vuelve a verificar
todo; sus respuestas e `Idempotent-Replayed` llegan sin alterar. El BFF no registra el cuerpo
(datos de salud) y nunca diagnostica. La app reintenta solo ante red, `502`, `503` o `504`,
con la misma clave (ADR-026, ADR-037).
"""

import logging
from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Body, Header, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from bff_mobile.clients.training_management import TrainingManagementClient
from bff_mobile.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.errors import ErrorResponse, error_response
from sportapp_platform.tokens import InvalidAccessToken, JwksUnavailable, VerifiedAccessToken

ATHLETE = "athlete"
BEARER_PREFIX = "bearer "
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
INSTANT = "Instante UTC con sufijo `Z` y precisión de segundos (`YYYY-MM-DDTHH:MM:SSZ`)."
# Cabeceras del servicio que el cliente necesita tal como llegan.
RELAYED_HEADERS = ("Cache-Control", REPLAYED_HEADER, "WWW-Authenticate")

_logger = logging.getLogger("sportapp.bff")

ReportType = Literal["DISCOMFORT", "INJURY", "INCAPACITY"]
BodyArea = Literal[
    "HEAD",
    "NECK",
    "SHOULDER",
    "ARM",
    "ELBOW",
    "WRIST_HAND",
    "CHEST",
    "BACK",
    "HIP",
    "THIGH",
    "KNEE",
    "LOWER_LEG",
    "ANKLE_FOOT",
    "OTHER",
]
Severity = Literal["LOW", "MODERATE", "HIGH"]


class HealthConsent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Annotated[
        str, Field(description="Igual a la versión vigente (`HEALTH_DATA_CONSENT_VERSION`).")
    ]


class ReportHealthRequest(BaseModel):
    """Esquema documentado del reporte; la validación la hace el servicio (ADR-037)."""

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
        UUID | None, Field(description="Sesión propia de entrenamiento (HU032); opcional.")
    ] = None
    expected_recovery_days: Annotated[int | None, Field(ge=0, le=365, description="Opcional.")] = (
        None
    )
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


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    schema.pop("$defs", None)
    return {"requestBody": {"required": True, "content": {"application/json": {"schema": schema}}}}


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


_ALLOWED: set[int] = {
    HTTPStatus.CREATED,
    HTTPStatus.UNAUTHORIZED,
    HTTPStatus.FORBIDDEN,
    HTTPStatus.NOT_FOUND,
    HTTPStatus.CONFLICT,
    HTTPStatus.UNPROCESSABLE_ENTITY,
    HTTPStatus.SERVICE_UNAVAILABLE,
}


def build_health_reports_router(
    *, verify_token: Callable[[str], VerifiedAccessToken], training: TrainingManagementClient
) -> APIRouter:
    router = APIRouter(prefix="/v1/me/health-reports", tags=["health-reports"])

    def authenticate_athlete(authorization: str | None) -> str | Response:
        if not authorization or not authorization.lower().startswith(BEARER_PREFIX):
            return _unauthenticated()
        token = authorization[len(BEARER_PREFIX) :].strip()
        try:
            identity = verify_token(token)
            UUID(identity.subject)
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
        return token

    @router.post(
        "",
        status_code=HTTPStatus.CREATED,
        response_model=HealthReportResponse,
        summary="Reportar una molestia, incapacidad o lesión (HU042)",
        openapi_extra=_schema(ReportHealthRequest),
        responses={
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
            HTTPStatus.UNAUTHORIZED.value: {
                "model": ErrorResponse,
                "description": "`invalid_access_token`: falta el token o no es válido.",
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
                "`field_not_allowed` (incluido `athlete_id`), `unknown_value`, `invalid_uuid`, "
                "`invalid_datetime`, `too_far_in_future`, `too_old`, `out_of_range` y "
                "`unsupported_consent_version`.",
            },
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": "`upstream_unavailable`: identidad no verificable o servicio caído.",
            },
            HTTPStatus.GATEWAY_TIMEOUT.value: {
                "model": ErrorResponse,
                "description": "`upstream_timeout`: Entrenamientos no respondió a tiempo.",
            },
        },
    )
    def report(
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = authenticate_athlete(authorization)
        if isinstance(token, Response):
            return token
        try:
            upstream = training.report_health(token, body, idempotency_key)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _failure(error)
        return _passthrough(upstream, _ALLOWED)

    return router
