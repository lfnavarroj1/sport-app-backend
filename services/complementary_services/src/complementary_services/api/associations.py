"""HU017: asociaciones del deportista a servicios médicos deportivos (ADR-038).

- `POST /v1/me/service-associations` con `{service_id}` e `Idempotency-Key`: `201` con la
  asociación `ACTIVE` y el resumen del servicio; `Idempotent-Replayed: true` en los
  reintentos; `409 association_exists` con la existente en `details`.
- `DELETE /v1/me/service-associations/{association_id}`: `204`, también al repetirlo.
- `GET /v1/me/service-associations?status&limit&cursor`: las propias, con historial.
Solo `athlete` con `services.sports_medicine.access` (verificada ante Afiliación); el
actor y la capacidad se informan antes que una solicitud mal formada. Asociar no comparte
datos de salud, no contrata, no reserva ni paga. Instantes en UTC (`Z`).
"""

import logging
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from complementary_services.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    forbidden,
)
from complementary_services.api.services import (
    IDEMPOTENCY_HEADER,
    REPLAYED_HEADER,
    ServicePrice,
    parse_idempotency_key,
    validation_failure,
)
from complementary_services.application.commands.associate_service import (
    AssociateService,
    AssociateServiceCommand,
    DissociateService,
)
from complementary_services.application.errors import (
    AssociationExists,
    AssociationNotFound,
    CapabilitiesTimeout,
    CapabilitiesUnavailable,
    CapabilityRequired,
    IdempotencyKeyReused,
    PermissionRequired,
    ServiceNotFound,
)
from complementary_services.application.ports import AssociationView
from complementary_services.application.queries.my_associations import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    ListMyAssociations,
    MyAssociationsFilters,
)
from complementary_services.application.queries.my_services import InvalidServicesQuery
from complementary_services.domain.catalog.offer import Category, Modality
from complementary_services.domain.validation import Issue, Violation
from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response

NO_STORE = "no-store"
BODY_FIELDS = ("service_id",)

_logger = logging.getLogger("sportapp.services")


class AssociateServiceRequest(BaseModel):
    """Esquema documentado del cuerpo; la validación la hace el transporte."""

    model_config = ConfigDict(extra="forbid")

    service_id: Annotated[
        UUID, Field(description="Servicio `PUBLISHED` de categoría `SPORTS_MEDICINE`.")
    ]


class AssociatedServiceSummary(BaseModel):
    """Resumen del servicio; la tarifa es informativa: asociar no contrata ni paga."""

    name: str
    category: Category
    modality: Modality
    fee: ServicePrice


class ServiceAssociationResponse(BaseModel):
    association_id: UUID
    service_id: UUID
    service: AssociatedServiceSummary
    status: Literal["ACTIVE", "INACTIVE"]
    associated_at: datetime
    ended_at: Annotated[
        datetime | None, Field(description="Instante de la desasociación; `null` si está activa.")
    ]
    version: int

    @classmethod
    def from_view(cls, view: AssociationView) -> "ServiceAssociationResponse":
        return cls.model_validate(view.to_document())


class ServiceAssociationsResponse(BaseModel):
    items: list[ServiceAssociationResponse]
    next_cursor: Annotated[
        str | None, Field(description="Cursor opaco de la página siguiente; `null` al final.")
    ]


def _request_schema() -> dict[str, Any]:
    return AssociateServiceRequest.model_json_schema(ref_template="#/components/schemas/{model}")


def parse_service_id(body: object) -> tuple[UUID | None, list[Violation]]:
    """`service_id` del cuerpo con los errores de forma del catálogo de ADR-027."""
    if not isinstance(body, dict):
        issue = Issue.REQUIRED if body is None else Issue.UNKNOWN_VALUE
        return None, [Violation(None, issue)]
    violations = [
        Violation(name, Issue.FIELD_NOT_ALLOWED) for name in body if name not in BODY_FIELDS
    ]
    raw = body.get("service_id")
    if raw is None:
        violations.append(Violation("service_id", Issue.REQUIRED))
        return None, violations
    if not isinstance(raw, str):
        violations.append(Violation("service_id", Issue.UNKNOWN_VALUE))
        return None, violations
    try:
        service_id = UUID(raw)
    except ValueError:
        violations.append(Violation("service_id", Issue.UNKNOWN_VALUE))
        return None, violations
    return (None if violations else service_id), violations


def _limit(raw: str | None) -> int | None:
    """`limit` entero; `None` si no es un entero (el rango lo valida la consulta)."""
    if raw is None:
        return DEFAULT_LIMIT
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _query_failure(field: str, issue: Issue) -> Response:
    return validation_failure([Violation(f"query.{field}", issue)])


def _access_failure(error: Exception) -> Response:
    if isinstance(error, PermissionRequired):
        return forbidden(error)
    if isinstance(error, CapabilityRequired):
        return error_response(
            HTTPStatus.FORBIDDEN, error.code, "Su plan no incluye esta funcionalidad."
        )
    _logger.warning(
        "services.capabilities_unavailable",
        extra={"fields": {"timeout": isinstance(error, CapabilitiesTimeout)}},
    )
    return error_response(
        HTTPStatus.SERVICE_UNAVAILABLE,
        CapabilitiesUnavailable.code,
        "No fue posible verificar el plan. Intente de nuevo.",
    )


_ACCESS_ERRORS = (PermissionRequired, CapabilityRequired, CapabilitiesUnavailable)
_ASSOCIATION_ERRORS = (ServiceNotFound, AssociationExists, IdempotencyKeyReused)


def _association_failure(error: Exception) -> Response:
    if isinstance(error, ServiceNotFound):
        return error_response(
            HTTPStatus.NOT_FOUND, error.code, "El servicio no existe o no se puede asociar."
        )
    if isinstance(error, AssociationExists):
        return error_response(
            HTTPStatus.CONFLICT,
            error.code,
            "Ya tiene una asociación activa con este servicio.",
            [ErrorDetail(field="association_id", issue=str(error.association_id))],
        )
    return error_response(
        HTTPStatus.CONFLICT,
        IdempotencyKeyReused.code,
        "La clave de idempotencia ya se usó con otra solicitud.",
    )


_RESPONSES: dict[int | str, dict[str, Any]] = {
    **AUTH_RESPONSES,
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required`: solo el deportista; `capability_required`: su "
        "plan no incluye `services.sports_medicine.access`.",
    },
    HTTPStatus.SERVICE_UNAVAILABLE.value: {
        "model": ErrorResponse,
        "description": "`upstream_unavailable`: no se pudo verificar la identidad o la "
        "capacidad ante Afiliación (también por timeout).",
    },
}


def build_associations_router(
    *,
    verify_token: TokenVerifier,
    associate_service: AssociateService,
    dissociate_service: DissociateService,
    list_my_associations: ListMyAssociations,
) -> APIRouter:
    router = APIRouter(prefix="/v1/me/service-associations", tags=["service-associations"])

    @router.post(
        "",
        status_code=HTTPStatus.CREATED,
        response_model=ServiceAssociationResponse,
        summary="Asociar un servicio médico deportivo (HU017)",
        description="Solo crea la relación: no comparte datos de salud con el profesional, no "
        "contrata, no reserva ni paga.",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": _request_schema()}},
            }
        },
        responses={
            **_RESPONSES,
            HTTPStatus.CREATED.value: {
                "headers": {
                    "Location": {"schema": {"type": "string"}},
                    REPLAYED_HEADER: {
                        "description": "`true` cuando se devuelve el resultado de un reintento.",
                        "schema": {"type": "string"},
                    },
                }
            },
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": "`service_not_found`: el servicio no existe, está en borrador o "
                "no es de `SPORTS_MEDICINE`.",
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`association_exists` con `details: [{field: "
                '"association_id", issue: <identificador de la activa>}]`, o '
                "`idempotency_key_reused`: la clave se usó con otro servicio.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` con `details[{field, issue}]`: `service_id` "
                "(`required`, `unknown_value`), otro campo (`field_not_allowed`), cuerpo "
                "ausente o que no es un objeto (`field: null`), `Idempotency-Key` ausente "
                "(`required`) o que no es un UUID (`unknown_value`).",
            },
        },
    )
    def associate(
        response: Response,
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | ServiceAssociationResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            associate_service.authorize(caller)
        except _ACCESS_ERRORS as error:
            return _access_failure(error)
        service_id, problems = parse_service_id(body)
        key, key_problem = parse_idempotency_key(idempotency_key)
        if key_problem is not None:
            problems.append(key_problem)
        if service_id is None or key is None:
            return validation_failure(problems)
        try:
            result = associate_service.execute(
                caller, AssociateServiceCommand(service_id=service_id, idempotency_key=key)
            )
        except _ASSOCIATION_ERRORS as error:
            return _association_failure(error)
        association_id = result.association.association.association_id
        response.headers["Location"] = f"/v1/me/service-associations/{association_id}"
        if result.replayed:
            response.headers[REPLAYED_HEADER] = "true"
        return ServiceAssociationResponse.from_view(result.association)

    @router.delete(
        "/{association_id}",
        status_code=HTTPStatus.NO_CONTENT,
        summary="Quitar una asociación propia (HU017)",
        description="La asociación pasa a `INACTIVE` con `ended_at` y se conserva en el "
        "historial; repetirlo responde `204` sin cambios.",
        response_class=Response,
        responses={
            **_RESPONSES,
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": "`association_not_found`: no existe o es de otro deportista.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `path.association_id` no es un UUID.",
            },
        },
    )
    def dissociate(
        association_id: UUID,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            dissociate_service(caller, association_id)
        except _ACCESS_ERRORS as error:
            return _access_failure(error)
        except AssociationNotFound as error:
            return error_response(
                HTTPStatus.NOT_FOUND, error.code, "La asociación no existe o no le pertenece."
            )
        return Response(status_code=HTTPStatus.NO_CONTENT)

    @router.get(
        "",
        response_model=ServiceAssociationsResponse,
        summary="Mis servicios asociados (HU017)",
        description="Ordenados por `associated_at` y `association_id` descendentes con cursor "
        "keyset; cada elemento trae el resumen del servicio.",
        responses={
            **_RESPONSES,
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `query.status` o `query.cursor` "
                "(`unknown_value`), `query.limit` (`unknown_value` u `out_of_range`).",
            },
        },
    )
    def my_associations(
        response: Response,
        status: Annotated[
            str | None,
            Query(description="`ACTIVE` (por defecto), `INACTIVE` o `ALL` (con el historial)."),
        ] = None,
        limit: Annotated[
            str | None,
            Query(description=f"Entero de 1 a {MAX_LIMIT}; {DEFAULT_LIMIT} por defecto."),
        ] = None,
        cursor: Annotated[str | None, Query(description="`next_cursor` anterior.")] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | ServiceAssociationsResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            list_my_associations.authorize(caller)
        except _ACCESS_ERRORS as error:
            return _access_failure(error)
        page_size = _limit(limit)
        if page_size is None:
            return _query_failure("limit", Issue.UNKNOWN_VALUE)
        try:
            page = list_my_associations.execute(
                caller, MyAssociationsFilters(status=status, limit=page_size, cursor=cursor)
            )
        except InvalidServicesQuery as error:
            return _query_failure(error.field, error.issue)
        response.headers["Cache-Control"] = NO_STORE
        return ServiceAssociationsResponse(
            items=[ServiceAssociationResponse.from_view(item) for item in page.items],
            next_cursor=page.next_cursor,
        )

    return router
