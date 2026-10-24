"""HU007 (ADR-036): `GET /v1/me/account-statement?limit&cursor`.

Estado de cuenta de quien consulta (el `user_id` sale del token; no existe consulta de cuentas
ajenas): suscripción vigente, compromisos de pago **informativos** y movimientos paginados.
SportApp no registra pagos: `payments_status` siempre `NOT_INTEGRATED` y `balance` siempre
`null`. Importes como cadena decimal en COP; instantes en UTC (`Z`).
"""

from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, Field

from affiliation_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    forbidden,
)
from affiliation_management.api.subscriptions import AMOUNT, INSTANT, SubscriptionResponse
from affiliation_management.application.errors import PermissionRequired
from affiliation_management.application.queries.account_statement import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    GetMyAccountStatement,
    InvalidStatementQuery,
    StatementQuery,
)
from affiliation_management.domain.subscriptions.model import Periodicity
from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response

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
            "`PROVISIONING` y `NOT_APPLICABLE` llevan listas vacías."
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
    *, verify_token: TokenVerifier, get_statement: GetMyAccountStatement
) -> APIRouter:
    router = APIRouter(tags=["account-statement"])

    @router.get(
        "/v1/me/account-statement",
        response_model=AccountStatementResponse,
        summary="Mi estado de cuenta (HU007)",
        description="Solo la cuenta propia, para `athlete`, `organizer` y `service_provider`. "
        f"{NO_PAYMENTS} Sin efectos.",
        responses={
            **AUTH_RESPONSES,
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
        *,
        response: Response,
        limit: Annotated[
            str | None, Query(description=f"1 a {MAX_LIMIT}; por defecto {DEFAULT_LIMIT}.")
        ] = None,
        cursor: Annotated[
            str | None, Query(max_length=512, description="`next_cursor` anterior.")
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | AccountStatementResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            parsed_limit = int(limit) if limit is not None else DEFAULT_LIMIT
        except ValueError:
            parsed_limit = 0  # Fuera de rango: lo informa la consulta como `out_of_range`.
        try:
            document = get_statement(caller, StatementQuery(limit=parsed_limit, cursor=cursor))
        except PermissionRequired:
            return forbidden()
        except InvalidStatementQuery as error:
            return error_response(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                error.code,
                "La consulta no es válida.",
                [ErrorDetail(field=f"query.{error.field}", issue=error.issue)],
            )
        response.headers["Cache-Control"] = "no-store"
        return AccountStatementResponse.model_validate(document)

    return router
