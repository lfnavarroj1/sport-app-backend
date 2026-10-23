"""HU016 (ADR-036): catálogo de planes, suscripción vigente y cambio de plan del deportista.

- `GET /v1/plans`: catálogo vigente con capacidades, ofertas, `selectable` y el plan actual.
- `GET /v1/me/subscription`: suscripción vigente o `PROVISIONING`.
- `POST /v1/me/subscription/changes`: cambio inmediato y sin prorrateo, con
  `Idempotency-Key` (24 h por deportista) y `expected_subscription_version`.

El `user_id` sale del token. SportApp **no cobra**: un plan pago registra un compromiso de pago
informativo `PENDING_EXTERNAL`. Importes como cadena decimal en COP; instantes en UTC (`Z`).
"""

from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Response
from pydantic import BaseModel, ConfigDict, Field

from affiliation_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    correlation_uuid,
    forbidden,
)
from affiliation_management.application.commands.change_subscription import (
    ChangeSubscription,
    ChangeSubscriptionCommand,
)
from affiliation_management.application.errors import IdempotencyKeyReused, PermissionRequired
from affiliation_management.application.queries.subscriptions import GetMySubscription, ListPlans
from affiliation_management.application.subscription_documents import format_amount
from affiliation_management.domain.subscriptions.change import (
    ChangeRejected,
    ChangeRequest,
    InconsistentPeriodicity,
    PlanNotFound,
    SubscriptionVersionConflict,
)
from affiliation_management.domain.subscriptions.model import Periodicity
from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
AMOUNT = "Cadena decimal con dos decimales en COP, antes de impuestos (precio de referencia)."
INSTANT = "Instante en UTC con sufijo `Z`."
NO_PAYMENT = "SportApp no procesa el pago: el compromiso es informativo."

SubscriptionStatusLiteral = Literal["ACTIVE", "PROVISIONING"]


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
    status: SubscriptionStatusLiteral
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


def _error(status: HTTPStatus, description: str) -> dict[int | str, dict[str, object]]:
    return {status.value: {"model": ErrorResponse, "description": description}}


_REJECTION_STATUS = {
    PlanNotFound.code: HTTPStatus.NOT_FOUND,
    InconsistentPeriodicity.code: HTTPStatus.UNPROCESSABLE_ENTITY,
}
_REJECTION_MESSAGES = {
    "plan_not_found": "El plan no existe en el catálogo vigente.",
    "plan_not_selectable": "El plan no se puede seleccionar con esa periodicidad.",
    "validation_error": "La periodicidad no corresponde al plan.",
    "subscription_provisioning": "Su plan gratuito aún se está activando. Intente de nuevo.",
    "subscription_unchanged": "Ya tiene ese plan con esa periodicidad.",
    "version_conflict": "Su suscripción cambió desde la última lectura; recargue.",
}


def _rejection(error: ChangeRejected) -> Response:
    status = _REJECTION_STATUS.get(error.code, HTTPStatus.CONFLICT)
    details: list[ErrorDetail] = []
    if isinstance(error, InconsistentPeriodicity):
        details = [ErrorDetail(field=error.field, issue=error.issue)]
    if isinstance(error, SubscriptionVersionConflict):
        details = [
            ErrorDetail(
                field="body.expected_subscription_version", issue=str(error.current_version)
            )
        ]
    return error_response(status, error.code, _REJECTION_MESSAGES[error.code], details)


def build_subscriptions_router(
    *,
    verify_token: TokenVerifier,
    list_plans: ListPlans,
    get_subscription: GetMySubscription,
    change_subscription: ChangeSubscription,
) -> APIRouter:
    router = APIRouter(tags=["subscriptions"])

    @router.get(
        "/v1/plans",
        response_model=PlansResponse,
        summary="Catálogo de planes con capacidades y ofertas (HU016)",
        responses=AUTH_RESPONSES,
    )
    def plans(
        response: Response, authorization: Annotated[str | None, Header()] = None
    ) -> Response | PlansResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            catalog = list_plans(caller)
        except PermissionRequired:
            return forbidden()
        response.headers["Cache-Control"] = "no-store"
        return PlansResponse(
            catalog_version=catalog.catalog_version,
            current_plan_code=catalog.current_plan_code,
            plans=[
                PlanResponse(
                    plan_code=plan.plan_code,
                    name=plan.name,
                    description=plan.description,
                    level=plan.level,
                    version=plan.version,
                    selectable=plan.selectable,
                    capabilities=list(plan.capabilities),
                    offers=[
                        PlanOfferResponse(
                            periodicity=Periodicity(offer.periodicity),
                            price=format_amount(offer.price),
                            currency="COP",
                            duration_months=offer.duration_months,
                        )
                        for offer in plan.offers
                    ],
                )
                for plan in catalog.plans
            ],
        )

    @router.get(
        "/v1/me/subscription",
        response_model=SubscriptionResponse,
        summary="Mi suscripción vigente (HU016)",
        responses=AUTH_RESPONSES,
    )
    def my_subscription(
        response: Response, authorization: Annotated[str | None, Header()] = None
    ) -> Response | SubscriptionResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            document = get_subscription(caller)
        except PermissionRequired:
            return forbidden()
        response.headers["Cache-Control"] = "no-store"
        return SubscriptionResponse.model_validate(document)

    @router.post(
        "/v1/me/subscription/changes",
        response_model=SubscriptionResponse,
        summary="Seleccionar o cambiar mi plan (HU016)",
        description="Cambio inmediato y sin prorrateo. Un plan pago registra un compromiso de "
        f"pago informativo `PENDING_EXTERNAL`; volver a `FREE` condona los vigentes. {NO_PAYMENT}",
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
        response: Response,
        idempotency_key: Annotated[UUID, Header(alias=IDEMPOTENCY_HEADER)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | SubscriptionResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        command = ChangeSubscriptionCommand(
            request=ChangeRequest(
                plan_code=body.plan_code,
                periodicity=body.periodicity,
                expected_subscription_version=body.expected_subscription_version,
            ),
            idempotency_key=idempotency_key,
            correlation_id=correlation_uuid(),
        )
        try:
            result = change_subscription(caller, command)
        except PermissionRequired:
            return forbidden()
        except ChangeRejected as error:
            return _rejection(error)
        except IdempotencyKeyReused as error:
            return error_response(
                HTTPStatus.CONFLICT,
                error.code,
                "La clave de idempotencia ya se usó con otra solicitud.",
            )
        response.headers["Cache-Control"] = "no-store"
        if result.replayed:
            response.headers[REPLAYED_HEADER] = "true"
        return SubscriptionResponse.model_validate(result.document)

    return router
