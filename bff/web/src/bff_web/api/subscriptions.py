"""HU016 en el canal web: catálogo, suscripción y cambio de plan del deportista (ADR-036).

El BFF autentica, filtra en el borde por actor (`athlete`, sin capacidad de plan) y reenvía
el token, el cuerpo y la clave de idempotencia sin reinterpretarlos: las reglas del cambio y
sus errores los decide `affiliation_management` y se entregan sin alterar. `GET
/v1/me/capabilities` no guarda caché (consulta Afiliación en cada solicitud), así que refleja el
cambio de inmediato y no hay nada que invalidar. SportApp no cobra: un plan pago solo registra
un compromiso de pago informativo.
"""

from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Response
from pydantic import BaseModel, ConfigDict, Field

from bff_web.api.training import (
    _UNAUTHORIZED,
    _UPSTREAM,
    ATHLETE,
    _passthrough,
    _permission_required,
    _upstream_failure,
    authenticate_session,
)
from bff_web.clients.affiliation_management import AffiliationManagementClient
from bff_web.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.errors import ErrorResponse
from sportapp_platform.tokens import VerifiedAccessToken

SOURCE = "affiliation-management"
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
AMOUNT = "Cadena decimal con dos decimales en COP, antes de impuestos (precio de referencia)."
INSTANT = "Instante en UTC con sufijo `Z`."
NO_PAYMENT = "SportApp no procesa el pago: el compromiso es informativo."

Periodicity = Literal["NONE", "MONTHLY", "QUARTERLY", "ANNUAL"]


class PlanOfferResponse(BaseModel):
    periodicity: Periodicity
    price: Annotated[str, Field(description=AMOUNT, examples=["2400.00"])]
    currency: Literal["COP"]
    duration_months: Annotated[int | None, Field(description="`null` en la oferta `NONE`.")]


class PlanResponse(BaseModel):
    plan_code: str
    name: str
    description: str
    level: int
    version: int
    selectable: Annotated[
        bool, Field(description="Los planes pagos; volver a `FREE` siempre está permitido.")
    ]
    capabilities: Annotated[
        list[str], Field(description="Capacidades materializadas (incluye las heredadas).")
    ]
    offers: list[PlanOfferResponse]


class PlansResponse(BaseModel):
    catalog_version: str
    current_plan_code: Annotated[
        str | None, Field(description="Plan vigente de quien consulta; `null` en `PROVISIONING`.")
    ]
    plans: list[PlanResponse]


class SubscriptionResponse(BaseModel):
    """Suscripción vigente; en `PROVISIONING` todos los demás campos son `null`."""

    subscription_id: UUID | None
    plan_code: str | None
    plan_version: int | None
    periodicity: Periodicity | None
    price: Annotated[
        str | None,
        Field(description=f"{AMOUNT} `null` si la versión del plan no tiene ofertas. {NO_PAYMENT}"),
    ]
    period_start: Annotated[datetime | None, Field(description=INSTANT)]
    period_end: Annotated[datetime | None, Field(description=f"{INSTANT} `null` en `FREE`.")]
    status: Literal["ACTIVE", "PROVISIONING"]
    origin: Literal["AUTO_REGISTRATION", "USER_CHANGE"] | None
    version: Annotated[
        int | None, Field(description="Se envía como `expected_subscription_version`.")
    ]


class SubscriptionChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_code: Annotated[str, Field(min_length=1, max_length=32, examples=["PREMIUM"])]
    periodicity: Annotated[
        Periodicity,
        Field(description="`NONE` para `FREE`; `MONTHLY`, `QUARTERLY` o `ANNUAL` para pagos."),
    ]
    expected_subscription_version: Annotated[
        int, Field(ge=1, description="`version` de la suscripción vigente.")
    ]


def _error(status: HTTPStatus, description: str) -> dict[int | str, dict[str, Any]]:
    return {status.value: {"model": ErrorResponse, "description": description}}


_FORBIDDEN = _error(HTTPStatus.FORBIDDEN, "`permission_required`: solo el deportista (`athlete`).")


def build_subscriptions_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    affiliation: AffiliationManagementClient,
) -> APIRouter:
    router = APIRouter(tags=["subscriptions"])

    def athlete_session(authorization: str | None) -> Any:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        if session.actor_type != ATHLETE:
            return _permission_required()
        return session

    def private(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get(
        "/v1/plans",
        response_model=PlansResponse,
        summary="Catálogo de planes con capacidades y ofertas (HU016)",
        responses={**_UNAUTHORIZED, **_FORBIDDEN, **_UPSTREAM},
    )
    def plans(authorization: Annotated[str | None, Header()] = None) -> Response:
        session = athlete_session(authorization)
        if isinstance(session, Response):
            return session
        try:
            upstream = affiliation.list_plans(session.token)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        return private(_passthrough(upstream, {HTTPStatus.OK, HTTPStatus.FORBIDDEN}, source=SOURCE))

    @router.get(
        "/v1/me/subscription",
        response_model=SubscriptionResponse,
        summary="Mi suscripción vigente (HU016)",
        description="En `PROVISIONING` (alta gratuita en curso) los demás campos son `null`.",
        responses={**_UNAUTHORIZED, **_FORBIDDEN, **_UPSTREAM},
    )
    def my_subscription(authorization: Annotated[str | None, Header()] = None) -> Response:
        session = athlete_session(authorization)
        if isinstance(session, Response):
            return session
        try:
            upstream = affiliation.my_subscription(session.token)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        return private(_passthrough(upstream, {HTTPStatus.OK, HTTPStatus.FORBIDDEN}, source=SOURCE))

    @router.post(
        "/v1/me/subscription/changes",
        response_model=SubscriptionResponse,
        summary="Seleccionar o cambiar mi plan (HU016)",
        description="Cambio inmediato y sin prorrateo. Un plan pago registra un compromiso de "
        f"pago informativo `PENDING_EXTERNAL`; volver a `FREE` condona los vigentes. {NO_PAYMENT} "
        "Tras el `200`, el cliente vuelve a consultar `GET /v1/me/capabilities`.",
        responses={
            **_UNAUTHORIZED,
            **_FORBIDDEN,
            **_UPSTREAM,
            HTTPStatus.OK.value: {
                "headers": {
                    REPLAYED_HEADER: {
                        "description": "`true` cuando se devuelve el resultado de un reintento.",
                        "schema": {"type": "string"},
                    }
                }
            },
            **_error(HTTPStatus.NOT_FOUND, "`plan_not_found`."),
            **_error(
                HTTPStatus.CONFLICT,
                "`plan_not_selectable`, `subscription_unchanged`, `subscription_provisioning` "
                "(recuperable), `version_conflict` (versión vigente en `details`) o "
                "`idempotency_key_reused`.",
            ),
            **_error(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "`validation_error`: forma del cuerpo o de `Idempotency-Key`, o periodicidad "
                "incoherente con el plan (`body.periodicity` `inconsistent_values`).",
            ),
        },
    )
    def change(
        body: SubscriptionChangeRequest,
        idempotency_key: Annotated[UUID, Header(alias=IDEMPOTENCY_HEADER)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = athlete_session(authorization)
        if isinstance(session, Response):
            return session
        try:
            upstream = affiliation.change_subscription(
                session.token, body.model_dump(mode="json"), str(idempotency_key)
            )
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        return private(
            _passthrough(
                upstream,
                {
                    HTTPStatus.OK,
                    HTTPStatus.FORBIDDEN,
                    HTTPStatus.NOT_FOUND,
                    HTTPStatus.CONFLICT,
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                },
                headers=(REPLAYED_HEADER,),
                source=SOURCE,
            )
        )

    return router
