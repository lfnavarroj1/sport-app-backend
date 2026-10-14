"""HU019: publicar servicios y consultar los servicios propios (ADR-027, ADR-038).

- `POST /v1/services/{service_id}/publish`: sin cuerpo, con `Idempotency-Key`; `200` con el
  estado de publicación vigente e `Idempotent-Replayed: true` en los reintentos.
- `GET /v1/me/services?status&limit&cursor` y `GET /v1/me/services/{service_id}`: la oferta
  completa de ADR-027 con `published_at`.
Solo el `service_provider` propietario; un servicio ajeno o inexistente es
`service_not_found`. Los instantes se responden en UTC (`Z`): el frontend los convierte.
"""

from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, Field

from complementary_services.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    correlation_uuid,
    forbidden,
)
from complementary_services.api.services import (
    IDEMPOTENCY_HEADER,
    REPLAYED_HEADER,
    ServiceResponse,
    parse_idempotency_key,
    validation_failure,
)
from complementary_services.application.commands.publish_service import (
    PublishService,
    PublishServiceCommand,
)
from complementary_services.application.errors import (
    IdempotencyKeyReused,
    PermissionRequired,
    ServiceNotFound,
)
from complementary_services.application.queries.my_services import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    GetMyService,
    InvalidServicesQuery,
    ListMyServices,
    MyServicesFilters,
)
from complementary_services.domain.validation import InvalidServiceOffer, Issue, Violation
from sportapp_platform.errors import ErrorResponse, error_response

NO_STORE = "no-store"


class PublicationResponse(BaseModel):
    service_id: UUID
    provider_id: UUID
    status: Literal["PUBLISHED"]
    version: int
    published_at: datetime
    updated_at: datetime


class MyServicesResponse(BaseModel):
    items: list[ServiceResponse]
    next_cursor: Annotated[
        str | None, Field(description="Cursor opaco de la página siguiente; `null` al final.")
    ]


_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    HTTPStatus.NOT_FOUND.value: {
        "model": ErrorResponse,
        "description": "`service_not_found`: el servicio no existe o es de otro profesional.",
    }
}
_PROVIDER_AUTH_RESPONSES: dict[int | str, dict[str, Any]] = {
    **AUTH_RESPONSES,
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required`: solo el profesional de servicios.",
    },
}


def _not_found(error: ServiceNotFound) -> Response:
    return error_response(
        HTTPStatus.NOT_FOUND, error.code, "El servicio no existe o no le pertenece."
    )


_PUBLICATION_ERRORS = (
    PermissionRequired,
    ServiceNotFound,
    InvalidServiceOffer,
    IdempotencyKeyReused,
)


def _publication_failure(error: Exception) -> Response:
    if isinstance(error, PermissionRequired):
        return forbidden(error)
    if isinstance(error, ServiceNotFound):
        return _not_found(error)
    if isinstance(error, InvalidServiceOffer):
        return validation_failure(error.violations)
    return error_response(
        HTTPStatus.CONFLICT,
        IdempotencyKeyReused.code,
        "La clave de idempotencia ya se usó con otra solicitud.",
    )


def _query_failure(field: str, issue: Issue) -> Response:
    return validation_failure([Violation(f"query.{field}", issue)])


def _limit(raw: str | None) -> int | None:
    """`limit` entero; `None` si no es un entero (el rango lo valida la consulta)."""
    if raw is None:
        return DEFAULT_LIMIT
    try:
        return int(raw.strip())
    except ValueError:
        return None


def build_publication_router(
    *,
    verify_token: TokenVerifier,
    publish_service: PublishService,
    list_my_services: ListMyServices,
    get_my_service: GetMyService,
) -> APIRouter:
    router = APIRouter(tags=["services"])

    @router.post(
        "/v1/services/{service_id}/publish",
        response_model=PublicationResponse,
        summary="Publicar un servicio propio en borrador (HU019)",
        description="Sin cuerpo. Un servicio ya publicado responde `200` con su estado vigente, "
        "sin subir la versión ni emitir otro `servicio.publicado.v1`.",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.OK.value: {
                "headers": {
                    REPLAYED_HEADER: {
                        "description": "`true` cuando se devuelve el resultado de un reintento.",
                        "schema": {"type": "string"},
                    }
                }
            },
            **_NOT_FOUND,
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`idempotency_key_reused`: la clave se usó con otra solicitud.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` con `details[{field, issue}]` (ADR-027): "
                "`path.service_id` no es un UUID, `Idempotency-Key` ausente o inválida, o la "
                "oferta no cumple las reglas de "
                'publicación (sin intervalos futuros: `{field: "availability", '
                'issue: "required"}`).',
            },
        },
    )
    def publish(
        service_id: UUID,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | PublicationResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            publish_service.authorize(caller)
            key, key_problem = parse_idempotency_key(idempotency_key)
            if key is None:
                return validation_failure([key_problem] if key_problem else [])
            result = publish_service(
                caller,
                PublishServiceCommand(
                    service_id=service_id, idempotency_key=key, correlation_id=correlation_uuid()
                ),
            )
        except _PUBLICATION_ERRORS as error:
            return _publication_failure(error)
        if result.replayed:
            response.headers[REPLAYED_HEADER] = "true"
        return PublicationResponse.model_validate(result.publication.to_document())

    @router.get(
        "/v1/me/services",
        response_model=MyServicesResponse,
        summary="Mis servicios (ADR-038, soporte de HU019 y HU018)",
        description="Ordenados por `updated_at` y `service_id` descendentes con cursor keyset.",
        responses={
            **_PROVIDER_AUTH_RESPONSES,
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `query.status` o `query.cursor` "
                "(`unknown_value`), `query.limit` (`unknown_value` u `out_of_range`).",
            },
        },
    )
    def my_services(
        response: Response,
        status: Annotated[str | None, Query(description="`DRAFT` o `PUBLISHED`.")] = None,
        limit: Annotated[
            str | None,
            Query(description=f"Entero de 1 a {MAX_LIMIT}; {DEFAULT_LIMIT} por defecto."),
        ] = None,
        cursor: Annotated[str | None, Query(description="`next_cursor` anterior.")] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | MyServicesResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        page_size = _limit(limit)
        try:
            # El permiso se informa antes que un parámetro mal formado.
            list_my_services.authorize(caller)
            if page_size is None:
                return _query_failure("limit", Issue.UNKNOWN_VALUE)
            page = list_my_services(
                caller, MyServicesFilters(status=status, limit=page_size, cursor=cursor)
            )
        except PermissionRequired as error:
            return forbidden(error)
        except InvalidServicesQuery as error:
            return _query_failure(error.field, error.issue)
        response.headers["Cache-Control"] = NO_STORE
        return MyServicesResponse(
            items=[ServiceResponse.from_view(item) for item in page.items],
            next_cursor=page.next_cursor,
        )

    @router.get(
        "/v1/me/services/{service_id}",
        response_model=ServiceResponse,
        summary="Detalle de un servicio propio (ADR-038, soporte de HU019 y HU018)",
        responses={
            **_PROVIDER_AUTH_RESPONSES,
            **_NOT_FOUND,
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `path.service_id` no es un UUID.",
            },
        },
    )
    def my_service(
        service_id: UUID,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | ServiceResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            view = get_my_service(caller, service_id)
        except PermissionRequired as error:
            return forbidden(error)
        except ServiceNotFound as error:
            return _not_found(error)
        response.headers["Cache-Control"] = NO_STORE
        return ServiceResponse.from_view(view)

    return router
