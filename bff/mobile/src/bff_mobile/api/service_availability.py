"""`GET /v1/services/availability` en el canal móvil (ADR-038; HU040, ADR-046).

Misma semántica que `complementary_services`: servicios `PUBLISHED` con sus intervalos futuros
que empiezan en `[from, to)`, en UTC (`Z`) con la zona declarada (el cliente convierte). Cada
intervalo trae su `availability_id` para reservar con `POST /v1/me/appointments`. El BFF
autentica y filtra el actor (`athlete`); los parámetros se reenvían tal como llegan (incluidos
`category` y `modality` repetidos) y el servicio decide la capacidad médica
(`restricted_categories`), los filtros y los `422`. La tarifa es informativa y la consulta no
reserva cupos.
"""

from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from bff_mobile.api.appointments import (
    _NO_STORE,
    ATHLETE_ONLY,
    UNAUTHORIZED,
    UPSTREAM,
    Category,
    Modality,
    ServicePrice,
    _private,
    athlete_session,
    relay,
)
from bff_mobile.clients.complementary_services import ComplementaryServicesClient
from sportapp_platform.errors import ErrorResponse
from sportapp_platform.tokens import VerifiedAccessToken

# Mismos límites documentados que el servicio (ADR-027); el BFF no los aplica.
CAPACITY_MIN, CAPACITY_MAX = 1, 100


class CatalogInterval(BaseModel):
    """Intervalo `[starts_at, ends_at)` en UTC con su zona y su identificador (HU040): se
    reserva con `POST /v1/me/appointments`."""

    model_config = ConfigDict(extra="forbid")

    starts_at: datetime
    ends_at: datetime
    time_zone: Annotated[str, Field(description="Zona IANA declarada, p. ej. America/Bogota.")]
    capacity: Annotated[int, Field(ge=CAPACITY_MIN, le=CAPACITY_MAX)]
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


_RESPONSES: dict[int | str, dict[str, Any]] = {
    **UNAUTHORIZED,
    **ATHLETE_ONLY,
    **UPSTREAM,
    HTTPStatus.OK.value: {"headers": _NO_STORE},
    HTTPStatus.UNPROCESSABLE_ENTITY.value: {
        "model": ErrorResponse,
        "description": "`validation_error` con `details[{field, issue}]`: `query.from`/`query.to` "
        "(`required`, `unknown_value`), `query.to` (`ends_before_starts`, `out_of_range` si el "
        "periodo supera 31 días), `query.category`, `query.modality`, `query.availability` y "
        "`query.cursor` (`unknown_value`), `query.limit` (`unknown_value`, `out_of_range`).",
    },
}


def build_service_availability_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    services: ComplementaryServicesClient,
) -> APIRouter:
    router = APIRouter(tags=["services"])

    @router.get(
        "/v1/services/availability",
        response_model=ServiceAvailabilityResponse,
        summary="Disponibilidad de los servicios publicados (HU020, ADR-038; HU040)",
        description="Solo el deportista. Ordenados por `name` y `service_id` con cursor keyset. "
        "Instantes en UTC con la zona del intervalo (sin parámetro de zona: el cliente "
        "convierte); la tarifa es informativa y la consulta no reserva cupos. Sin "
        "`services.sports_medicine.access`, la categoría médica se omite y se declara en "
        "`restricted_categories`.",
        responses=_RESPONSES,
    )
    def service_availability(
        *,
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
        limit: Annotated[str | None, Query(description="Entero de 1 a 50; 20 por defecto.")] = None,
        cursor: Annotated[str | None, Query(description="`next_cursor` anterior.")] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        caller = athlete_session(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        token = caller[0]
        # Tal como llegan, incluidos los repetidos: el servicio valida y responde el `422`.
        single = (
            ("from", starts_from),
            ("to", starts_before),
            ("availability", availability),
            ("limit", limit),
            ("cursor", cursor),
        )
        params = [(name, value) for name, value in single if value is not None]
        params += [("category", value) for value in category or ()]
        params += [("modality", value) for value in modality or ()]
        return _private(
            relay(
                lambda: services.service_availability(token, params),
                {
                    HTTPStatus.OK,
                    HTTPStatus.UNAUTHORIZED,
                    HTTPStatus.FORBIDDEN,
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    HTTPStatus.SERVICE_UNAVAILABLE,
                },
            )
        )

    return router
