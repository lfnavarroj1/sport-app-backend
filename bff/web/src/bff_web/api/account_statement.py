"""HU007 en el canal web: estado de cuenta de quien consulta (ADR-036).

El BFF autentica, deja pasar a los tres actores del canal (`athlete`, `organizer` y
`service_provider`; otro actor → `permission_required`) y reenvía el token, `limit` y `cursor`
tal como llegaron: `statement_status`, compromisos, movimientos, paginación y errores los decide
`affiliation_management` y se entregan sin alterar. No hay parámetro de usuario: solo la cuenta
propia. SportApp no registra pagos (`payments_status = NOT_INTEGRATED`, `balance = null`).
"""

from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, Field

from bff_web.api.subscriptions import AMOUNT, INSTANT, SOURCE, Periodicity, SubscriptionResponse
from bff_web.api.training import (
    _UNAUTHORIZED,
    _UPSTREAM,
    _passthrough,
    _permission_required,
    _upstream_failure,
    authenticate_session,
)
from bff_web.clients.affiliation_management import AffiliationManagementClient
from bff_web.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.errors import ErrorResponse
from sportapp_platform.tokens import VerifiedAccessToken

ACTORS = frozenset({"athlete", "organizer", "service_provider"})
NO_PAYMENTS = "SportApp no registra pagos: la información financiera no está confirmada."


class CommitmentItem(BaseModel):
    """Compromiso de pago informativo: SportApp no lo cobra ni lo marca pagado."""

    commitment_id: UUID
    plan_code: str
    periodicity: Literal["MONTHLY", "QUARTERLY", "ANNUAL"]
    amount: Annotated[str, Field(description=AMOUNT, examples=["2400.00"])]
    currency: Literal["COP"]
    period_start: Annotated[datetime, Field(description=INSTANT)]
    period_end: Annotated[datetime, Field(description=INSTANT)]
    due_at: Annotated[datetime, Field(description=f"{INSTANT} Pago anticipado: inicio.")]
    status: Annotated[
        Literal["PENDING_EXTERNAL", "WAIVED"],
        Field(description="`WAIVED`: condonado al volver a `FREE` antes del fin del periodo."),
    ]


class MovementItem(BaseModel):
    movement_id: UUID
    movement_type: Literal[
        "FREE_ACTIVATED",
        "PLAN_CHANGED",
        "RETURNED_TO_FREE",
        "COMMITMENT_CREATED",
        "COMMITMENT_WAIVED",
    ]
    occurred_at: Annotated[datetime, Field(description=INSTANT)]
    subscription_id: UUID
    plan_code: str
    periodicity: Periodicity
    commitment_id: Annotated[
        UUID | None, Field(description="Solo en `COMMITMENT_CREATED` y `COMMITMENT_WAIVED`.")
    ]
    amount: Annotated[
        str | None, Field(description=f"{AMOUNT} Importe del compromiso; `null` en los demás.")
    ]
    currency: Annotated[Literal["COP"] | None, Field(description="`null` sin importe.")]


class AccountStatementResponse(BaseModel):
    statement_status: Annotated[
        Literal["ACTIVE", "PROVISIONING", "NOT_APPLICABLE", "NONE"],
        Field(
            description="`NOT_APPLICABLE`: organizador o profesional; `PROVISIONING`: alta "
            "gratuita en curso; `NONE`: sin suscripción activa pero con historial. "
            "`PROVISIONING` y `NOT_APPLICABLE` llevan listas vacías; ninguno es un error."
        ),
    ]
    subscription: Annotated[
        SubscriptionResponse | None,
        Field(description="La vigente (como `GET /v1/me/subscription`); `null` sin activa."),
    ]
    commitments: Annotated[
        list[CommitmentItem],
        Field(
            description="De la suscripción vigente y de las anteriores cuyo periodo no terminó; "
            "orden `period_start` descendente."
        ),
    ]
    movements: Annotated[
        list[MovementItem],
        Field(description="Página de movimientos, orden `occurred_at DESC, movement_id DESC`."),
    ]
    next_cursor: str | None = Field(
        description="Cursor opaco de la página siguiente de movimientos; `null` si no hay más."
    )
    payments_status: Annotated[Literal["NOT_INTEGRATED"], Field(description=NO_PAYMENTS)]
    balance: Annotated[None, Field(description="Siempre `null`: no hay dominio de pagos (R-13).")]


def build_account_statement_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    affiliation: AffiliationManagementClient,
) -> APIRouter:
    router = APIRouter(tags=["account-statement"])

    @router.get(
        "/v1/me/account-statement",
        response_model=AccountStatementResponse,
        summary="Mi estado de cuenta (HU007)",
        description="Solo la cuenta propia, para `athlete`, `organizer` y `service_provider`. "
        f"{NO_PAYMENTS} Sin efectos.",
        responses={
            **_UNAUTHORIZED,
            **_UPSTREAM,
            HTTPStatus.FORBIDDEN.value: {
                "model": ErrorResponse,
                "description": "`permission_required`: tipo de actor ausente o desconocido.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `query.limit` `out_of_range` o "
                "`query.cursor` `invalid_cursor`.",
            },
        },
    )
    def account_statement(
        limit: Annotated[str | None, Query(description="1 a 100; por defecto 20.")] = None,
        cursor: Annotated[
            str | None, Query(max_length=512, description="`next_cursor` anterior.")
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        if session.actor_type not in ACTORS:
            return _permission_required()
        params = {
            name: value
            for name, value in (("limit", limit), ("cursor", cursor))
            if value is not None
        }
        try:
            upstream = affiliation.account_statement(session.token, params)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        response = _passthrough(
            upstream,
            {HTTPStatus.OK, HTTPStatus.FORBIDDEN, HTTPStatus.UNPROCESSABLE_ENTITY},
            source=SOURCE,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    return router
