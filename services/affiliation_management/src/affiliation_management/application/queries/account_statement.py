"""HU007 (ADR-036): `GET /v1/me/account-statement?limit&cursor` de quien consulta.

Aplica a los tres actores y solo a la cuenta propia (el `user_id` sale del token; CA3); un
actor desconocido recibe `permission_required` (ADR-025).
`organizer` y `service_provider` reciben `NOT_APPLICABLE` sin leer la base. Movimientos
paginados con cursor keyset opaco, orden `occurred_at DESC, movement_id DESC` (CA5);
`limit` de 1 a 100. Sin pagos: `payments_status = NOT_INTEGRATED` y `balance = null` (CA4).
Sin efectos: no escribe nada.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from affiliation_management.application.errors import PermissionRequired
from affiliation_management.application.ports import (
    AccountStatementReader,
    Caller,
    Clock,
    MovementPosition,
    MovementView,
)
from affiliation_management.application.subscription_documents import (
    format_amount,
    subscription_document,
)
from affiliation_management.domain.subscriptions.account_statement import (
    PAYMENTS_NOT_INTEGRATED,
    StatementStatus,
    listed_commitments,
    statement_status,
)
from affiliation_management.domain.subscriptions.change import BillingCommitment
from affiliation_management.domain.subscriptions.events import utc_instant
from affiliation_management.domain.subscriptions.model import ATHLETE, KNOWN_ACTOR_TYPES

MAX_LIMIT = 100
DEFAULT_LIMIT = 20


class InvalidStatementQuery(Exception):
    """`422 validation_error` en `query.<field>`."""

    code = "validation_error"

    def __init__(self, field: str, issue: str) -> None:
        super().__init__(f"{field}: {issue}")
        self.field = field
        self.issue = issue


@dataclass(frozen=True)
class StatementQuery:
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None


def encode_cursor(position: MovementPosition) -> str:
    raw = json.dumps(
        {"t": position.occurred_at.isoformat(), "m": str(position.movement_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> MovementPosition:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        occurred_at = datetime.fromisoformat(document["t"])
        movement_id = UUID(document["m"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        raise InvalidStatementQuery("cursor", "invalid_cursor") from None
    if occurred_at.tzinfo is None:
        raise InvalidStatementQuery("cursor", "invalid_cursor")
    return MovementPosition(occurred_at=occurred_at, movement_id=movement_id)


def _amount(value: Any) -> str | None:
    return format_amount(value) if value is not None else None


def commitment_document(commitment: BillingCommitment) -> dict[str, Any]:
    return {
        "commitment_id": str(commitment.commitment_id),
        "plan_code": commitment.plan_code,
        "periodicity": commitment.periodicity.value,
        "amount": format_amount(commitment.amount),
        "currency": commitment.currency,
        "period_start": utc_instant(commitment.period_start),
        "period_end": utc_instant(commitment.period_end),
        "due_at": utc_instant(commitment.due_at),
        "status": commitment.status.value,
    }


def movement_document(view: MovementView) -> dict[str, Any]:
    movement = view.movement
    return {
        "movement_id": str(movement.movement_id),
        "movement_type": movement.movement_type.value,
        "occurred_at": utc_instant(movement.occurred_at),
        "subscription_id": str(movement.subscription_id),
        "plan_code": movement.plan_code,
        "periodicity": movement.periodicity.value,
        "commitment_id": str(movement.commitment_id) if movement.commitment_id else None,
        "amount": _amount(view.amount),
        "currency": view.currency,
    }


def _statement(
    status: StatementStatus,
    *,
    subscription: dict[str, Any] | None = None,
    commitments: list[dict[str, Any]] | None = None,
    movements: list[dict[str, Any]] | None = None,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    return {
        "statement_status": status.value,
        "subscription": subscription,
        "commitments": commitments or [],
        "movements": movements or [],
        "next_cursor": next_cursor,
        "payments_status": PAYMENTS_NOT_INTEGRATED,
        "balance": None,
    }


class GetMyAccountStatement:
    def __init__(self, *, reader: AccountStatementReader, clock: Clock) -> None:
        self._reader = reader
        self._clock = clock

    def __call__(self, caller: Caller, query: StatementQuery) -> dict[str, Any]:
        if caller.actor_type not in KNOWN_ACTOR_TYPES:
            raise PermissionRequired()
        if not 1 <= query.limit <= MAX_LIMIT:
            raise InvalidStatementQuery("limit", "out_of_range")
        after = decode_cursor(query.cursor) if query.cursor else None
        if caller.actor_type != ATHLETE:
            return _statement(StatementStatus.NOT_APPLICABLE)
        snapshot = self._reader.read(caller.user_id, after=after, limit=query.limit + 1)
        status = statement_status(
            caller.actor_type,
            has_active_subscription=snapshot.current is not None,
            has_any_subscription=snapshot.has_any_subscription,
        )
        if status is StatementStatus.PROVISIONING:
            return _statement(status)
        current = snapshot.current
        commitments = listed_commitments(
            snapshot.commitments,
            current_subscription_id=current.subscription.subscription_id if current else None,
            now=self._clock.now(),
        )
        page = snapshot.movements[: query.limit]
        next_cursor = (
            encode_cursor(
                MovementPosition(page[-1].movement.occurred_at, page[-1].movement.movement_id)
            )
            if len(snapshot.movements) > query.limit
            else None
        )
        return _statement(
            status,
            subscription=subscription_document(current.subscription, current.price)
            if current
            else None,
            commitments=[commitment_document(commitment) for commitment in commitments],
            movements=[movement_document(view) for view in page],
            next_cursor=next_cursor,
        )
