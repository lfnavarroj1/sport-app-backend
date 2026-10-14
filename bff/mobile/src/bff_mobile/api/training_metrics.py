"""HU034 en el canal móvil: indicadores FTP y VO₂max estimados (contrato de ADR-030).

El BFF autentica (JWKS), exige `athlete` y la capacidad vigente `training.metrics.ftp_vo2max`
(Afiliación) antes de llamar a Entrenamientos; después reenvía cuerpo, `Idempotency-Key`,
`If-None-Match`, parámetros y correlación sin reinterpretarlos. El deportista sale del token:
el BFF no calcula ni acepta `athlete_id` (el servicio lo rechaza con `field_not_allowed`).
Las fórmulas, el catálogo, la idempotencia y sus errores los decide `training_management`,
que vuelve a verificar todo; sus respuestas, `ETag` e `Idempotent-Replayed` llegan sin
alterar. La app reintenta solo ante red, `502`, `503` o `504`, con la misma clave.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Body, Header, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from bff_mobile.clients.training_management import TrainingManagementClient
from bff_mobile.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.errors import ErrorResponse, error_response
from sportapp_platform.tokens import InvalidAccessToken, JwksUnavailable, VerifiedAccessToken

ATHLETE = "athlete"
BEARER_PREFIX = "bearer "
METRICS_CAPABILITY = "training.metrics.ftp_vo2max"
IDEMPOTENCY_HEADER = "Idempotency-Key"
IF_NONE_MATCH_HEADER = "If-None-Match"
REPLAYED_HEADER = "Idempotent-Replayed"
INSTANT = "Instante UTC con sufijo `Z` y precisión de segundos (`YYYY-MM-DDTHH:MM:SSZ`)."
# Cabeceras del servicio que el cliente necesita tal como llegan.
RELAYED_HEADERS = ("ETag", "Cache-Control", "Location", REPLAYED_HEADER, "WWW-Authenticate")

_logger = logging.getLogger("sportapp.bff")


class CatalogInput(BaseModel):
    code: str
    label_key: str
    value_type: Literal["INTEGER"]
    unit: str
    minimum: int
    maximum: int
    scale: int
    required: bool


class CatalogMethod(BaseModel):
    metric_code: str
    label_key: str
    result_unit: str
    result_scale: int
    method_code: str
    method_version: int
    method_label_key: str
    disclaimer_key: str
    inputs: list[CatalogInput]


class TrainingMetricCatalogResponse(BaseModel):
    """Métodos activos con metadatos para renderizar formulario y resultado sin lógica local."""

    catalog_version: str
    methods: list[CatalogMethod]


class MetricInputValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    value: Annotated[int, Field(description="Entero dentro de `minimum`..`maximum` del catálogo.")]
    unit: str


class CalculateTrainingMetricRequest(BaseModel):
    """Esquema documentado del cálculo; la validación la hace el servicio (ADR-030)."""

    model_config = ConfigDict(extra="forbid")

    calculation_id: Annotated[UUID, Field(description="UUID v4 generado por el móvil.")]
    metric_code: str
    method_code: str
    method_version: int
    measured_at: Annotated[datetime, Field(description=INSTANT, examples=["2026-10-12T15:30:00Z"])]
    inputs: list[MetricInputValue]


class MetricResult(BaseModel):
    value: Annotated[
        int | float,
        Field(description="Entero con escala 0; con `scale` decimales en otro caso."),
    ]
    unit: str
    scale: int


class TrainingMetricCalculationResponse(BaseModel):
    """Cálculo confirmado: estimación de campo, nunca un diagnóstico."""

    calculation_id: UUID
    athlete_id: UUID
    metric_code: str
    method_code: str
    method_version: int
    measured_at: datetime
    inputs: list[MetricInputValue]
    result: MetricResult
    status: Literal["CONFIRMED"]
    calculated_at: datetime
    disclaimer_key: str
    version: int


class TrainingMetricCalculationsResponse(BaseModel):
    """Resultados propios proyectados, `measured_at DESC, calculation_id DESC`."""

    items: list[TrainingMetricCalculationResponse]
    next_cursor: str | None


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
    if upstream.status_code == HTTPStatus.NOT_MODIFIED:
        return Response(status_code=HTTPStatus.NOT_MODIFIED, headers=headers)
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
        "description": "`permission_required` (no es deportista) o `capability_required` "
        f"(`{METRICS_CAPABILITY}`).",
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
_ETAG = {"ETag": {"description": '`"<catalog_version>"`.', "schema": {"type": "string"}}}
_REPLAY = {
    REPLAYED_HEADER: {
        "description": "`true` cuando se devuelve el resultado original de la misma clave.",
        "schema": {"type": "string"},
    }
}
_EDGE_STATUSES = {
    HTTPStatus.UNAUTHORIZED,
    HTTPStatus.FORBIDDEN,
    HTTPStatus.SERVICE_UNAVAILABLE,
    HTTPStatus.GATEWAY_TIMEOUT,
}


def build_training_metrics_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    athlete_snapshot: Callable[[UUID], dict[str, Any]],
    training: TrainingManagementClient,
) -> APIRouter:
    router = APIRouter(prefix="/v1/training-metrics", tags=["training-metrics"])

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

    def require_capability(user_id: UUID) -> Response | None:
        try:
            snapshot = athlete_snapshot(user_id)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _failure(error)
        granted = snapshot.get("capabilities") or []
        if snapshot.get("subscription_status") == "ACTIVE" and METRICS_CAPABILITY in granted:
            return None
        return error_response(
            HTTPStatus.FORBIDDEN, "capability_required", "Su plan no incluye esta funcionalidad."
        )

    def authorized_token(authorization: str | None) -> str | Response:
        """Borde: actor y capacidad antes de llamar a Entrenamientos."""
        caller = authenticate_athlete(authorization)
        if isinstance(caller, Response):
            return caller
        user_id, token = caller
        rejection = require_capability(user_id)
        return rejection if rejection is not None else token

    def relay(send: Callable[[], httpx.Response], allowed: set[int]) -> Response:
        try:
            upstream = send()
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _failure(error)
        return _passthrough(upstream, allowed)

    @router.get(
        "/catalog",
        response_model=TrainingMetricCatalogResponse,
        summary="Catálogo de métodos de FTP y VO₂max estimados (HU034)",
        responses={
            **_COMMON,
            HTTPStatus.OK.value: {"headers": _ETAG},
            HTTPStatus.NOT_MODIFIED.value: {
                "description": "`If-None-Match` coincide con el catálogo vigente; sin cuerpo.",
                "headers": _ETAG,
            },
        },
    )
    def catalog(
        if_none_match: Annotated[str | None, Header(alias=IF_NONE_MATCH_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = authorized_token(authorization)
        if isinstance(token, Response):
            return token
        return relay(
            lambda: training.metrics_catalog(token, if_none_match),
            {HTTPStatus.OK, HTTPStatus.NOT_MODIFIED, *_EDGE_STATUSES},
        )

    @router.post(
        "/calculations",
        status_code=HTTPStatus.CREATED,
        response_model=TrainingMetricCalculationResponse,
        summary="Calcular un FTP o VO₂max estimado (HU034)",
        openapi_extra=_schema(CalculateTrainingMetricRequest),
        responses={
            **_COMMON,
            HTTPStatus.CREATED.value: {"headers": _REPLAY},
            HTTPStatus.OK.value: {
                "model": TrainingMetricCalculationResponse,
                "description": "El mismo `calculation_id` con los mismos datos y otra clave: "
                "resultado existente, sin otro evento.",
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`idempotency_key_reused` o `calculation_id_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` con `details[{field, issue}]`: `required`, "
                "`field_not_allowed`, `unknown_metric`, `unsupported_method_version`, "
                "`unknown_input`, `duplicate_input`, `invalid_unit`, `out_of_range`, "
                "`invalid_datetime`, `too_far_in_future` o `too_old` (ADR-030); "
                "`invalid_uuid` para `calculation_id` o `Idempotency-Key` que no son UUID.",
            },
        },
    )
    def calculate(
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = authorized_token(authorization)
        if isinstance(token, Response):
            return token
        return relay(
            lambda: training.calculate_metric(token, body, idempotency_key),
            {
                HTTPStatus.CREATED,
                HTTPStatus.OK,
                HTTPStatus.CONFLICT,
                HTTPStatus.UNPROCESSABLE_ENTITY,
                *_EDGE_STATUSES,
            },
        )

    @router.get(
        "/calculations",
        response_model=TrainingMetricCalculationsResponse,
        summary="Mis resultados de FTP y VO₂max estimados (HU034, lectura CQRS)",
        responses={
            **_COMMON,
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `unknown_metric`, `limit` o `cursor` "
                "inválidos.",
            },
        },
    )
    def calculations(
        *,
        metric_code: Annotated[str | None, Query()] = None,
        limit: Annotated[str | None, Query(description="1 a 100; por defecto 20.")] = None,
        cursor: Annotated[str | None, Query(description="Cursor opaco.")] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        token = authorized_token(authorization)
        if isinstance(token, Response):
            return token
        params = {
            name: value
            for name, value in (("metric_code", metric_code), ("limit", limit), ("cursor", cursor))
            if value is not None
        }
        response = relay(
            lambda: training.metric_calculations(token, params),
            {HTTPStatus.OK, HTTPStatus.UNPROCESSABLE_ENTITY, *_EDGE_STATUSES},
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    return router
