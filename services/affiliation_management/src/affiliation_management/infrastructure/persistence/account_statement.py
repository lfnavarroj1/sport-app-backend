"""HU007 (ADR-036): lectura del estado de cuenta en la base única de Afiliación.

Suscripción vigente, compromisos y una página de `subscription_history` se leen en una sola
transacción `REPEATABLE READ`, así que un cambio de plan concurrente no mezcla dos estados.
Los movimientos siguen el índice `ix_subscription_history_user` con keyset
`(occurred_at, movement_id) < cursor` en orden descendente. Solo lectura.
"""

from uuid import UUID

from sqlalchemy import exists, select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from affiliation_management.application.ports import (
    AccountStatementSnapshot,
    MovementPosition,
    MovementView,
)
from affiliation_management.domain.subscriptions.change import Movement, MovementType
from affiliation_management.domain.subscriptions.model import Periodicity
from affiliation_management.infrastructure.persistence.models import (
    BillingCommitmentRow,
    SubscriptionHistoryRow,
    SubscriptionRow,
)
from affiliation_management.infrastructure.persistence.subscriptions import (
    _commitment,
    active_view,
)


class SqlAlchemyAccountStatementReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def read(
        self, user_id: UUID, *, after: MovementPosition | None, limit: int
    ) -> AccountStatementSnapshot:
        with self._session_factory() as session:
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            return AccountStatementSnapshot(
                current=active_view(session, user_id),
                has_any_subscription=bool(
                    session.scalar(select(exists().where(SubscriptionRow.user_id == user_id)))
                ),
                commitments=[
                    _commitment(row)
                    for row in session.scalars(
                        select(BillingCommitmentRow).where(BillingCommitmentRow.user_id == user_id)
                    )
                ],
                movements=_movements(session, user_id, after, limit),
            )


def _movements(
    session: Session, user_id: UUID, after: MovementPosition | None, limit: int
) -> list[MovementView]:
    query = (
        select(SubscriptionHistoryRow, BillingCommitmentRow.amount, BillingCommitmentRow.currency)
        .outerjoin(
            BillingCommitmentRow,
            BillingCommitmentRow.commitment_id == SubscriptionHistoryRow.commitment_id,
        )
        .where(SubscriptionHistoryRow.user_id == user_id)
    )
    if after is not None:
        query = query.where(
            tuple_(SubscriptionHistoryRow.occurred_at, SubscriptionHistoryRow.movement_id)
            < tuple_(after.occurred_at, after.movement_id)
        )
    rows = session.execute(
        query.order_by(
            SubscriptionHistoryRow.occurred_at.desc(), SubscriptionHistoryRow.movement_id.desc()
        ).limit(limit)
    ).all()
    return [
        MovementView(
            movement=Movement(
                movement_id=row.movement_id,
                user_id=row.user_id,
                movement_type=MovementType(row.movement_type),
                occurred_at=row.occurred_at,
                subscription_id=row.subscription_id,
                plan_code=row.plan_code,
                periodicity=Periodicity(row.periodicity),
                commitment_id=row.commitment_id,
            ),
            amount=amount,
            currency=currency,
        )
        for row, amount, currency in rows
    ]
