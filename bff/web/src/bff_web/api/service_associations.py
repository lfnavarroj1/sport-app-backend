"""HU017 en el canal web: asociar servicios médicos deportivos (ADR-038).

`POST /v1/me/service-associations`, `DELETE /v1/me/service-associations/{association_id}`
y `GET /v1/me/service-associations`. El BFF autentica, filtra el actor (`athlete`) y
consulta la capacidad `services.sports_medicine.access` ante Afiliación (sin ella
`403 capability_required`; sin respuesta de Afiliación `503 upstream_unavailable`). El
cuerpo, la clave de idempotencia y los parámetros se reenvían sin reinterpretarlos:
`complementary_services` vuelve a verificar token, actor y capacidad y decide la
elegibilidad, la unicidad y los `422`. Asociar no comparte datos de salud, no contrata, no
reserva ni paga. Los instantes llegan en UTC (`Z`); el frontend los convierte.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from bff_web.api.services import (
    IDEMPOTENCY_HEADER,
    REPLAYED_HEADER,
    SOURCE,
    Category,
    Modality,
    ServicePrice,
)
from bff_web.api.training import (
    _UNAUTHORIZED,
    _UPSTREAM,
    ATHLETE,
    _bearer_token,
    _passthrough,
    _permission_required,
    _Session,
    _unauthenticated,
    _upstream_failure,
)
from bff_web.clients.affiliation_management import AffiliationManagementClient
from bff_web.clients.complementary_services import ComplementaryServicesClient
from bff_web.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.errors import ErrorResponse, error_response
from sportapp_platform.tokens import InvalidAccessToken, JwksUnavailable, VerifiedAccessToken

MEDICAL_CAPABILITY = "services.sports_medicine.access"
CAPABILITY_REQUIRED = "capability_required"

_logger = logging.getLogger("sportapp.bff")


class AssociateServiceRequest(BaseModel):
    """Esquema documentado del cuerpo; la validación la hace el servicio."""

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
        datetime | None, Field(description="UTC (`Z`); `null` mientras está activa.")
    ]
    version: int


class ServiceAssociationsResponse(BaseModel):
    items: list[ServiceAssociationResponse]
    next_cursor: str | None


_FORBIDDEN: dict[int | str, dict[str, Any]] = {
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required`: solo el deportista; `capability_required`: su "
        "plan no incluye `services.sports_medicine.access`.",
    }
}
_CAPABILITY_UNAVAILABLE: dict[int | str, dict[str, Any]] = {
    HTTPStatus.SERVICE_UNAVAILABLE.value: {
        "model": ErrorResponse,
        "description": "`upstream_unavailable`: Afiliación no permitió verificar la capacidad "
        "(también por timeout) o un servicio interno no está disponible.",
    }
}


def _request_schema() -> dict[str, Any]:
    return AssociateServiceRequest.model_json_schema(ref_template="#/components/schemas/{model}")


def _private(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def build_service_associations_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    affiliation: AffiliationManagementClient,
    services: ComplementaryServicesClient,
) -> APIRouter:
    router = APIRouter(tags=["service-associations"])

    def athlete(authorization: str | None) -> tuple[_Session, UUID] | Response:
        """Sesión verificada del deportista y su identificador."""
        token = _bearer_token(authorization)
        if token is None:
            return _unauthenticated()
        try:
            identity = verify_token(token)
            user_id = UUID(identity.subject)
        except (InvalidAccessToken, ValueError):
            return _unauthenticated()
        except JwksUnavailable:
            _logger.warning("bff.jwks_unavailable")
            return _upstream_failure(UpstreamUnavailable())
        if identity.actor_type != ATHLETE:
            return _permission_required()
        return _Session(token, identity), user_id

    def medical_athlete(authorization: str | None) -> _Session | Response:
        """Deportista con la capacidad médica vigente según Afiliación."""
        caller = athlete(authorization)
        if isinstance(caller, Response):
            return caller
        session, user_id = caller
        try:
            snapshot = affiliation.user_capabilities(user_id)
        except (UpstreamTimeout, UpstreamUnavailable, ValueError) as error:
            # ADR-038: la capacidad no evaluable es `503`, también por timeout; nunca se
            # concede por suposición.
            _logger.warning(
                "bff.capabilities_unavailable",
                extra={"fields": {"timeout": isinstance(error, UpstreamTimeout)}},
            )
            return _upstream_failure(UpstreamUnavailable())
        granted = snapshot.get("capabilities") or []
        if snapshot.get("subscription_status") != "ACTIVE" or MEDICAL_CAPABILITY not in granted:
            return error_response(
                HTTPStatus.FORBIDDEN,
                CAPABILITY_REQUIRED,
                "Su plan no incluye esta funcionalidad.",
            )
        return session

    @router.post(
        "/v1/me/service-associations",
        status_code=HTTPStatus.CREATED,
        response_model=ServiceAssociationResponse,
        summary="Asociar un servicio médico deportivo (HU017, ADR-038)",
        description="Solo crea la relación: no comparte datos de salud con el profesional, no "
        "contrata, no reserva ni paga. Una asociación activa por deportista y servicio.",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": _request_schema()}},
            }
        },
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            **_FORBIDDEN,
            **_CAPABILITY_UNAVAILABLE,
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
                "description": "`service_not_found`: no existe, está en borrador o no es de "
                "`SPORTS_MEDICINE`.",
            },
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`association_exists` con `details: [{field: "
                '"association_id", issue: <identificador de la activa>}]`, o '
                "`idempotency_key_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` con `details[{field, issue}]`: `service_id`, "
                "campos de más, cuerpo ausente o `Idempotency-Key` ausente o inválida.",
            },
        },
    )
    def associate(
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[
            str | None,
            Header(
                alias=IDEMPOTENCY_HEADER, description="UUID; obligatorio (lo valida el servicio)."
            ),
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = medical_athlete(authorization)
        if isinstance(session, Response):
            return session
        try:
            upstream = services.associate_service(session.token, body, idempotency_key)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        return _passthrough(
            upstream,
            {
                HTTPStatus.CREATED,
                HTTPStatus.UNAUTHORIZED,
                HTTPStatus.FORBIDDEN,
                HTTPStatus.NOT_FOUND,
                HTTPStatus.CONFLICT,
                HTTPStatus.UNPROCESSABLE_ENTITY,
                HTTPStatus.SERVICE_UNAVAILABLE,
            },
            headers=("Location", REPLAYED_HEADER),
            source=SOURCE,
        )

    @router.delete(
        "/v1/me/service-associations/{association_id}",
        status_code=HTTPStatus.NO_CONTENT,
        response_class=Response,
        summary="Quitar una asociación propia (HU017, ADR-038)",
        description="Pasa a `INACTIVE` con `ended_at` y se conserva en el historial; "
        "repetirlo responde `204`.",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            **_FORBIDDEN,
            **_CAPABILITY_UNAVAILABLE,
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
        session = medical_athlete(authorization)
        if isinstance(session, Response):
            return session
        try:
            upstream = services.dissociate_service(session.token, association_id)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        return _passthrough(
            upstream,
            {
                HTTPStatus.NO_CONTENT,
                HTTPStatus.UNAUTHORIZED,
                HTTPStatus.FORBIDDEN,
                HTTPStatus.NOT_FOUND,
                HTTPStatus.SERVICE_UNAVAILABLE,
            },
            source=SOURCE,
        )

    @router.get(
        "/v1/me/service-associations",
        response_model=ServiceAssociationsResponse,
        summary="Mis servicios asociados (HU017, ADR-038)",
        description="Del más reciente al más antiguo, con el resumen del servicio.",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            **_FORBIDDEN,
            **_CAPABILITY_UNAVAILABLE,
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` en `query.status`, `query.limit` o "
                "`query.cursor`.",
            },
        },
    )
    def list_mine(
        *,
        status: Annotated[
            str | None,
            Query(description="`ACTIVE` (por defecto), `INACTIVE` o `ALL` (con el historial)."),
        ] = None,
        limit: Annotated[
            str | None, Query(description="Entero de 1 a 100; 20 por defecto.")
        ] = None,
        cursor: Annotated[
            str | None, Query(description="`next_cursor` de la página anterior.")
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = medical_athlete(authorization)
        if isinstance(session, Response):
            return session
        # Solo los presentes y tal como llegan: el servicio valida y responde el `422`.
        pairs = (("status", status), ("limit", limit), ("cursor", cursor))
        params = {name: value for name, value in pairs if value is not None}
        try:
            upstream = services.list_my_associations(session.token, params)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        return _private(
            _passthrough(
                upstream,
                {
                    HTTPStatus.OK,
                    HTTPStatus.UNAUTHORIZED,
                    HTTPStatus.FORBIDDEN,
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    HTTPStatus.SERVICE_UNAVAILABLE,
                },
                source=SOURCE,
            )
        )

    return router
