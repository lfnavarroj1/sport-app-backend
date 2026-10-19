"""HU034: indicadores FTP y VO₂max (contrato de ADR-030).

- `GET /v1/training-metrics/catalog`: catálogo versionado con `ETag`; `If-None-Match`
  coincidente responde `304` sin cuerpo.
- `POST /v1/training-metrics/calculations`: calcula (`201`); la misma clave devuelve el
  resultado original (`201` + `Idempotent-Replayed: true`) y el mismo `calculation_id`
  con los mismos datos y otra clave devuelve el existente (`200`).
- `GET /v1/training-metrics/calculations`: historial propio proyectado con cursor.

Las tres exigen `athlete` con `training.metrics.ftp_vo2max`; el deportista sale del token.
El cuerpo del cálculo se lee como JSON sin validación automática para responder el
catálogo de causas de ADR-030 (`details[{field, issue}]`); su esquema se documenta en el
OpenAPI. Instantes en UTC con sufijo `Z`; valores como números JSON.
"""

from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from training_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    correlation_uuid,
    forbidden,
)
from training_management.application.commands.calculate_training_metric import (
    IDEMPOTENCY_HEADER,
    CalculateTrainingMetric,
    CalculateTrainingMetricCommand,
    CalculationOutcome,
)
from training_management.application.errors import (
    CalculationIdReused,
    CapabilitiesTimeout,
    CapabilitiesUnavailable,
    CapabilityRequired,
    IdempotencyKeyReused,
    PermissionRequired,
)
from training_management.application.metric_documents import calculation_document
from training_management.application.queries.training_metrics import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    GetMetricCatalog,
    InvalidMetricResultQuery,
    ListTrainingMetricResults,
    MetricResultFilters,
    MetricResultPage,
)
from training_management.domain.metrics.catalog import MetricCatalog, quantum
from training_management.domain.metrics.validation import InvalidMetricCalculation

REPLAYED_HEADER = "Idempotent-Replayed"
INSTANT = "Instante UTC con sufijo `Z` y precisión de segundos (`YYYY-MM-DDTHH:MM:SSZ`)."

_ERRORS: dict[type[Exception], tuple[HTTPStatus, str]] = {
    IdempotencyKeyReused: (
        HTTPStatus.CONFLICT,
        "La clave de idempotencia ya se usó con otra solicitud.",
    ),
    CalculationIdReused: (
        HTTPStatus.CONFLICT,
        "El identificador del cálculo ya se usó con otros datos.",
    ),
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
    InvalidMetricCalculation,
    *_ERRORS,
)


class MetricInputDefinition(BaseModel):
    code: str
    label_key: str
    value_type: Literal["INTEGER"]
    unit: str
    minimum: int
    maximum: int
    scale: int
    required: bool


class MetricMethod(BaseModel):
    metric_code: str
    label_key: str
    result_unit: str
    result_scale: int
    method_code: str
    method_version: int
    method_label_key: str
    disclaimer_key: str
    inputs: list[MetricInputDefinition]


class MetricCatalogResponse(BaseModel):
    catalog_version: str
    methods: list[MetricMethod]

    @classmethod
    def from_catalog(cls, catalog: MetricCatalog) -> "MetricCatalogResponse":
        return cls(
            catalog_version=catalog.catalog_version,
            methods=[
                MetricMethod(
                    metric_code=method.metric_code,
                    label_key=method.label_key,
                    result_unit=method.result_unit,
                    result_scale=method.result_scale,
                    method_code=method.method_code,
                    method_version=method.method_version,
                    method_label_key=method.method_label_key,
                    disclaimer_key=method.disclaimer_key,
                    inputs=[
                        MetricInputDefinition(
                            code=item.code,
                            label_key=item.label_key,
                            value_type="INTEGER",
                            unit=item.unit,
                            minimum=int(item.minimum.quantize(quantum(0))),
                            maximum=int(item.maximum.quantize(quantum(0))),
                            scale=item.scale,
                            required=item.required,
                        )
                        for item in method.inputs
                    ],
                )
                for method in catalog.methods
            ],
        )


class MetricInputValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Annotated[str, Field(description="Código de la entrada del método en el catálogo.")]
    value: Annotated[int, Field(description="Entero dentro de `minimum`..`maximum`.")]
    unit: Annotated[str, Field(description="Unidad exacta del catálogo.")]


class CalculateTrainingMetricRequest(BaseModel):
    """Esquema documentado del cálculo; la validación la hace el dominio (ADR-030)."""

    model_config = ConfigDict(extra="forbid")

    calculation_id: Annotated[UUID, Field(description="UUID v4 generado por el móvil.")]
    metric_code: Annotated[str, Field(examples=["FTP_ESTIMATED"])]
    method_code: Annotated[str, Field(examples=["FTP_20_MIN_95_V1"])]
    method_version: Annotated[int, Field(examples=[1])]
    measured_at: Annotated[datetime, Field(description=INSTANT, examples=["2026-10-12T15:30:00Z"])]
    inputs: list[MetricInputValue]


class MetricResultValue(BaseModel):
    value: Annotated[
        int | float,
        Field(description="Entero con escala 0; con `scale` decimales en otro caso."),
    ]
    unit: str
    scale: int


class TrainingMetricCalculationResponse(BaseModel):
    """Estimación confirmada; nunca es un diagnóstico (`estimated_not_diagnostic`)."""

    calculation_id: UUID
    athlete_id: UUID
    metric_code: str
    method_code: str
    method_version: int
    measured_at: datetime
    inputs: list[MetricInputValue]
    result: MetricResultValue
    status: Literal["CONFIRMED"]
    calculated_at: datetime
    disclaimer_key: str
    version: int


class TrainingMetricCalculationsResponse(BaseModel):
    items: list[TrainingMetricCalculationResponse]
    next_cursor: str | None = Field(
        description="Cursor opaco de la página siguiente; `null` si no hay más."
    )

    @classmethod
    def from_page(cls, page: MetricResultPage) -> "TrainingMetricCalculationsResponse":
        return cls(
            items=[
                TrainingMetricCalculationResponse.model_validate(calculation_document(item))
                for item in page.items
            ],
            next_cursor=page.next_cursor,
        )


def _request_body() -> dict[str, Any]:
    schema = CalculateTrainingMetricRequest.model_json_schema(
        ref_template="#/components/schemas/{model}"
    )
    schema.pop("$defs", None)
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": schema}},
        }
    }


def _failure(error: Exception) -> Response:
    if isinstance(error, PermissionRequired | CapabilityRequired):
        return forbidden(error)
    if isinstance(error, InvalidMetricCalculation):
        return error_response(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            error.code,
            "La solicitud no es válida.",
            [ErrorDetail(field=item.field, issue=item.issue.value) for item in error.violations],
        )
    status, message = _ERRORS[type(error)]
    return error_response(status, getattr(error, "code", ""), message)


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    candidates = {item.strip().removeprefix("W/") for item in if_none_match.split(",")}
    return "*" in candidates or etag in candidates


_FORBIDDEN = {
    "model": ErrorResponse,
    "description": "`permission_required` (no es deportista) o `capability_required` "
    "(`training.metrics.ftp_vo2max`).",
}
_TIMEOUT = {
    "model": ErrorResponse,
    "description": "`upstream_timeout`: Afiliación no respondió a tiempo.",
}
_REPLAY_HEADERS = {
    REPLAYED_HEADER: {
        "description": "`true` cuando se devuelve el resultado original de la misma clave.",
        "schema": {"type": "string"},
    }
}


def _limit(raw: str | None) -> int:
    """`limit` como entero; uno no entero se informa como `out_of_range` (ADR-030), igual que
    un valor fuera de 1..100, al validarlo el caso de uso."""
    if raw is None:
        return DEFAULT_LIMIT
    try:
        return int(raw.strip())
    except ValueError:
        return 0


def build_training_metrics_router(
    *,
    verify_token: TokenVerifier,
    get_catalog: GetMetricCatalog,
    calculate: CalculateTrainingMetric,
    list_results: ListTrainingMetricResults,
) -> APIRouter:
    router = APIRouter(prefix="/v1/training-metrics", tags=["training-metrics"])

    @router.get(
        "/catalog",
        response_model=MetricCatalogResponse,
        summary="Catálogo de métodos de indicadores (HU034)",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.OK.value: {
                "headers": {
                    "ETag": {
                        "description": '`"<catalog_version>"`.',
                        "schema": {"type": "string"},
                    }
                }
            },
            HTTPStatus.NOT_MODIFIED.value: {
                "description": "`If-None-Match` coincide con el `ETag` vigente; sin cuerpo."
            },
            HTTPStatus.FORBIDDEN.value: _FORBIDDEN,
            HTTPStatus.GATEWAY_TIMEOUT.value: _TIMEOUT,
        },
    )
    def catalog(
        response: Response,
        if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | MetricCatalogResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            current = get_catalog(caller)
        except _HANDLED as error:
            return _failure(error)
        if _etag_matches(if_none_match, current.etag):
            return Response(status_code=HTTPStatus.NOT_MODIFIED, headers={"ETag": current.etag})
        response.headers["ETag"] = current.etag
        return MetricCatalogResponse.from_catalog(current)

    @router.post(
        "/calculations",
        status_code=HTTPStatus.CREATED,
        response_model=TrainingMetricCalculationResponse,
        summary="Calcular un indicador estimado (HU034)",
        openapi_extra=_request_body(),
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.OK.value: {
                "model": TrainingMetricCalculationResponse,
                "description": "El `calculation_id` ya existe con los mismos datos (otra "
                "clave): se devuelve sin otro evento.",
            },
            HTTPStatus.CREATED.value: {"headers": _REPLAY_HEADERS},
            HTTPStatus.FORBIDDEN.value: _FORBIDDEN,
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`idempotency_key_reused` o `calculation_id_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` con `details[{field, issue}]` y las causas "
                "de ADR-030 (`required`, `field_not_allowed`, `unknown_metric`, "
                "`unsupported_method_version`, `unknown_input`, `duplicate_input`, "
                "`invalid_unit`, `out_of_range`, `invalid_datetime`, `too_far_in_future`, "
                "`too_old`) más `invalid_uuid` para `calculation_id` e `Idempotency-Key`.",
            },
            HTTPStatus.GATEWAY_TIMEOUT.value: _TIMEOUT,
        },
    )
    def calculate_metric(
        response: Response,
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | TrainingMetricCalculationResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            result = calculate(
                caller,
                CalculateTrainingMetricCommand(
                    body=body, idempotency_key=idempotency_key, correlation_id=correlation_uuid()
                ),
            )
        except _HANDLED as error:
            return _failure(error)
        if result.outcome is CalculationOutcome.EXISTING:
            response.status_code = HTTPStatus.OK
        elif result.outcome is CalculationOutcome.REPLAYED:
            response.headers[REPLAYED_HEADER] = "true"
        return TrainingMetricCalculationResponse.model_validate(
            calculation_document(result.calculation)
        )

    @router.get(
        "/calculations",
        response_model=TrainingMetricCalculationsResponse,
        summary="Mis resultados de indicadores (HU034, lectura CQRS)",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.FORBIDDEN.value: _FORBIDDEN,
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `query.metric_code` (`unknown_metric`), "
                "`query.limit` (`out_of_range`, también si no es un entero) o `query.cursor` "
                "(`invalid_cursor`).",
            },
            HTTPStatus.GATEWAY_TIMEOUT.value: _TIMEOUT,
        },
    )
    def calculations(
        *,
        response: Response,
        metric_code: Annotated[str | None, Query(description="Código del catálogo.")] = None,
        limit: Annotated[
            str | None,
            Query(description=f"Entero de 1 a {MAX_LIMIT}; {DEFAULT_LIMIT} por defecto."),
        ] = None,
        cursor: Annotated[str | None, Query(description="`next_cursor` anterior.")] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | TrainingMetricCalculationsResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            page = list_results(
                caller,
                MetricResultFilters(metric_code=metric_code, limit=_limit(limit), cursor=cursor),
            )
        except _HANDLED as error:
            return _failure(error)
        except InvalidMetricResultQuery as error:
            return error_response(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                error.code,
                "La consulta no es válida.",
                [ErrorDetail(field=f"query.{error.field}", issue=error.issue)],
            )
        response.headers["Cache-Control"] = "no-store"
        return TrainingMetricCalculationsResponse.from_page(page)

    return router
