"""HU020: `GET /v1/services/availability` (ADR-038) para el deportista.

Servicios `PUBLISHED` con sus intervalos futuros que empiezan en `[from, to)`, en UTC (`Z`)
con la zona declarada: el frontend convierte a la zona del usuario. La categoría médica
exige `services.sports_medicine.access` (Afiliación); sin ella se declara en
`restricted_categories`. La tarifa es informativa y la lectura no reserva cupos. HU040 agrega
el `availability_id` de cada intervalo para reservar una sesión virtual.
"""

import logging
from http import HTTPStatus
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, Field

from complementary_services.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    forbidden,
)
from complementary_services.api.services import (
    AvailabilityInterval,
    ServicePrice,
    validation_failure,
)
from complementary_services.application.errors import (
    CapabilitiesTimeout,
    CapabilitiesUnavailable,
    PermissionRequired,
)
from complementary_services.application.queries.my_services import InvalidServicesQuery
from complementary_services.application.queries.service_availability import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    AvailabilityFilters,
    ListServiceAvailability,
)
from complementary_services.domain.catalog.offer import Category, Modality, ServiceOffer
from complementary_services.domain.catalog.publication import utc_instant
from complementary_services.domain.validation import Issue, Violation
from sportapp_platform.errors import ErrorResponse, error_response

NO_STORE = "no-store"

_logger = logging.getLogger("sportapp.services")


class CatalogInterval(AvailabilityInterval):
    """Intervalo del catálogo con su identificador (HU040): se reserva con
    `POST /v1/me/appointments`."""

    availability_id: UUID


class CatalogService(BaseModel):
    service_id: UUID
    provider_id: UUID
    name: str
    description: str
    category: Category
    modality: Modality
    price: Annotated[ServicePrice, Field(description="Tarifa informativa: no es un cobro.")]
    availability: Annotated[
        list[CatalogInterval],
        Field(description="Intervalos futuros que empiezan en el rango, en UTC con su zona."),
    ]
    available: Annotated[
        bool, Field(description="`false` solo con `availability=all` y sin intervalos.")
    ]


class ServiceAvailabilityResponse(BaseModel):
    items: list[CatalogService]
    next_cursor: Annotated[
        str | None, Field(description="Cursor opaco de la página siguiente; `null` al final.")
    ]
    restricted_categories: Annotated[
        list[Category],
        Field(
            description="Categorías omitidas por falta de capacidad del plan "
            '(`["SPORTS_MEDICINE"]` sin `services.sports_medicine.access`).'
        ),
    ]


def _item(offer: ServiceOffer) -> dict[str, Any]:
    return {
        "service_id": str(offer.service_id),
        "provider_id": str(offer.provider_id),
        "name": offer.name,
        "description": offer.description,
        "category": offer.category.value,
        "modality": offer.modality.value,
        "price": {
            "amount": str(offer.price.amount),
            "currency": offer.price.currency.value,
            "billing_unit": offer.price.billing_unit.value,
        },
        "availability": [
            {
                "starts_at": utc_instant(interval.starts_at),
                "ends_at": utc_instant(interval.ends_at),
                "time_zone": interval.time_zone,
                "capacity": interval.capacity,
                "availability_id": str(interval.availability_id),
            }
            for interval in offer.availability
        ],
        "available": bool(offer.availability),
    }


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


_RESPONSES: dict[int | str, dict[str, Any]] = {
    **AUTH_RESPONSES,
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required`: solo el deportista.",
    },
    HTTPStatus.UNPROCESSABLE_ENTITY.value: {
        "model": ErrorResponse,
        "description": "`validation_error` con `details[{field, issue}]`: `query.from`/`query.to` "
        "(`required`, `unknown_value`), `query.to` (`ends_before_starts`, `out_of_range` si el "
        "periodo supera 31 días), `query.category`, `query.modality`, `query.availability` y "
        "`query.cursor` (`unknown_value`), `query.limit` (`unknown_value`, `out_of_range`).",
    },
    HTTPStatus.SERVICE_UNAVAILABLE.value: {
        "model": ErrorResponse,
        "description": "`upstream_unavailable`: no se pudo verificar la identidad o la "
        "capacidad médica ante Afiliación (también por timeout).",
    },
}


def build_availability_router(
    *, verify_token: TokenVerifier, list_availability: ListServiceAvailability
) -> APIRouter:
    router = APIRouter(tags=["services"])

    @router.get(
        "/v1/services/availability",
        response_model=ServiceAvailabilityResponse,
        summary="Disponibilidad de los servicios publicados (HU020)",
        description="Ordenados por `name` y `service_id` con cursor keyset. Sin parámetro de zona "
        "horaria: los instantes van en UTC con la zona del intervalo.",
        responses=_RESPONSES,
    )
    def service_availability(
        *,
        response: Response,
        starts_from: Annotated[
            str | None,
            Query(alias="from", description="Instante ISO 8601 con desfase; obligatorio."),
        ] = None,
        starts_before: Annotated[
            str | None,
            Query(
                alias="to",
                description="Instante ISO 8601 con desfase, posterior a `from` y a no más de "
                "31 días; obligatorio.",
            ),
        ] = None,
        category: Annotated[
            list[str] | None, Query(description="Repetible; sin valor, todas las permitidas.")
        ] = None,
        modality: Annotated[list[str] | None, Query(description="Repetible.")] = None,
        availability: Annotated[
            str | None, Query(description="`available` (por defecto) o `all`.")
        ] = None,
        limit: Annotated[
            str | None,
            Query(description=f"Entero de 1 a {MAX_LIMIT}; {DEFAULT_LIMIT} por defecto."),
        ] = None,
        cursor: Annotated[str | None, Query(description="`next_cursor` anterior.")] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | ServiceAvailabilityResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        page_size = _limit(limit)
        try:
            # El actor se informa antes que un parámetro mal formado.
            list_availability.authorize(caller)
            if page_size is None:
                return _query_failure("limit", Issue.UNKNOWN_VALUE)
            page = list_availability(
                caller,
                AvailabilityFilters(
                    starts_from=starts_from,
                    starts_before=starts_before,
                    categories=tuple(category or ()),
                    modalities=tuple(modality or ()),
                    availability=availability,
                    limit=page_size,
                    cursor=cursor,
                ),
            )
        except PermissionRequired as error:
            return forbidden(error)
        except InvalidServicesQuery as error:
            return _query_failure(error.field, error.issue)
        except CapabilitiesUnavailable as error:
            _logger.warning(
                "services.capabilities_unavailable",
                extra={"fields": {"timeout": isinstance(error, CapabilitiesTimeout)}},
            )
            return error_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                error.code,
                "No fue posible verificar el plan. Intente de nuevo.",
            )
        response.headers["Cache-Control"] = NO_STORE
        return ServiceAvailabilityResponse.model_validate(
            {
                "items": [_item(offer) for offer in page.items],
                "next_cursor": page.next_cursor,
                "restricted_categories": [item.value for item in page.restricted_categories],
            }
        )

    return router
