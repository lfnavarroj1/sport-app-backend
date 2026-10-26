"""HU025 en el canal web: tarifa informativa de un evento propio (contrato de ADR-040).

- `PUT /v1/events/{event_id}/fee` (`{amount, currency, expected_version}`) y
  `DELETE /v1/events/{event_id}/fee?expected_version=N`, ambos con `Idempotency-Key`.
- Filtro de borde: `organizer` con `events.pricing.manage`. Cuerpo, parámetro y clave viajan
  tal cual: el servicio decide propiedad (`404`), versión (`409`), reglas de importe,
  moneda e inicio (`422` con `details`) e idempotencia, y responde el agregado con
  `registration_count` sin esperar la proyección.
Los errores del servicio se entregan sin alterar. La tarifa no es un cobro.
"""

from collections.abc import Callable
from http import HTTPStatus
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from bff_web.api.event_publication import OwnEventResponse
from bff_web.api.training import (
    _UNAUTHORIZED,
    _UPSTREAM,
    _passthrough,
    _permission_required,
    _upstream_failure,
    authenticate_session,
)
from bff_web.clients.events_management import EventsManagementClient
from bff_web.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.authorization import ActorPermissions
from sportapp_platform.errors import ErrorResponse
from sportapp_platform.tokens import VerifiedAccessToken

ORGANIZER = "organizer"
PRICING_PERMISSION = "events.pricing.manage"
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
SOURCE = "events-management"
_FORWARDED: set[int] = {
    HTTPStatus.OK,
    HTTPStatus.FORBIDDEN,
    HTTPStatus.NOT_FOUND,
    HTTPStatus.CONFLICT,
    HTTPStatus.UNPROCESSABLE_ENTITY,
}


class EventFeeRequest(BaseModel):
    """Esquema documentado del cuerpo; la validación la hacen el transporte y el dominio."""

    model_config = ConfigDict(extra="forbid")

    amount: Annotated[
        str,
        Field(
            pattern=r"^[0-9]+\.[0-9]{2}$",
            description="Cadena decimal con exactamente dos decimales, de `0.00` (gratuito) a "
            "`99999999.99`.",
            examples=["85000.00"],
        ),
    ]
    currency: Annotated[str, Field(description="Solo `COP`.", examples=["COP"])]
    expected_version: Annotated[
        int, Field(ge=1, description="Versión vigente del evento que vio el organizador.")
    ]


def _request_schema() -> dict[str, Any]:
    schema = EventFeeRequest.model_json_schema(ref_template="#/components/schemas/{model}")
    schema.pop("$defs", None)
    return schema


def _error(status: HTTPStatus, description: str) -> dict[int | str, dict[str, Any]]:
    return {status.value: {"model": ErrorResponse, "description": description}}


_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_UNAUTHORIZED,
    **_UPSTREAM,
    HTTPStatus.OK.value: {
        "headers": {
            REPLAYED_HEADER: {
                "description": "`true` cuando se devuelve el resultado de un reintento.",
                "schema": {"type": "string"},
            }
        }
    },
    **_error(
        HTTPStatus.FORBIDDEN, "`permission_required` (`organizer` con `events.pricing.manage`)."
    ),
    **_error(HTTPStatus.NOT_FOUND, "`event_not_found`: inexistente o de otro organizador."),
    **_error(
        HTTPStatus.CONFLICT,
        "`version_conflict` (`details[0]` = `{field: expected_version, issue: <versión "
        "vigente>}`) o `idempotency_key_reused`.",
    ),
}


def build_event_fee_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    permissions: ActorPermissions,
    events: EventsManagementClient,
    channel: str,
) -> APIRouter:
    router = APIRouter(prefix="/v1/events/{event_id}/fee", tags=["events"])

    def forward(
        authorization: str | None,
        send: Callable[[str], Any],
    ) -> Response:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        actor = session.actor_type
        if actor != ORGANIZER or PRICING_PERMISSION not in permissions.for_actor(actor, channel):
            return _permission_required()
        try:
            upstream = send(session.token)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        return _passthrough(upstream, _FORWARDED, headers=(REPLAYED_HEADER,), source=SOURCE)

    @router.put(
        "",
        response_model=OwnEventResponse,
        summary="Asignar la tarifa de un evento propio (HU025)",
        description="`DRAFT` o `PUBLISHED` antes de `starts_at`; `0.00` es gratuito. Responde "
        "la versión incrementada y `registration_count`; las inscripciones existentes "
        "conservan su `applied_fee`. No genera cobro, estado de pago ni reembolso.",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": _request_schema()}},
            }
        },
        responses={
            **_RESPONSES,
            **_error(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "`validation_error` con `details[{field, issue}]`: `amount` (`required`, "
                "`unknown_value` sin dos decimales, `too_many_decimals`, `out_of_range`), "
                "`currency` (`unsupported_currency`), `expected_version`, `field_not_allowed`, "
                "`starts_at` (`event_already_started`) o `Idempotency-Key` ausente o inválida.",
            ),
        },
    )
    def put_fee(
        event_id: UUID,
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        return forward(
            authorization,
            lambda token: events.put_event_fee(token, event_id, body, idempotency_key),
        )

    @router.delete(
        "",
        response_model=OwnEventResponse,
        summary="Retirar la tarifa de un evento propio (HU025)",
        description="Deja `fee = null` con las mismas reglas de estado, inicio y versión que "
        "la asignación.",
        responses={
            **_RESPONSES,
            **_error(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "`validation_error` con `details[{field, issue}]`: `query.expected_version` "
                "(`required`, `unknown_value`, `out_of_range`), `starts_at` "
                "(`event_already_started`) o `Idempotency-Key` ausente o inválida.",
            ),
        },
    )
    def delete_fee(
        event_id: UUID,
        expected_version: Annotated[
            str | None,
            Query(description="Versión vigente del evento que vio el organizador (entero ≥ 1)."),
        ] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        params = {} if expected_version is None else {"expected_version": expected_version}
        return forward(
            authorization,
            lambda token: events.delete_event_fee(token, event_id, params, idempotency_key),
        )

    return router
