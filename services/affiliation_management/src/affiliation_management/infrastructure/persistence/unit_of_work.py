"""Unidad de trabajo SQLAlchemy del alta gratuita (inbox + suscripción + historial + outbox)."""

from datetime import datetime
from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy import exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from affiliation_management.application.ports import (
    ActiveSubscriptionExists,
    DuplicateDelivery,
    InboxRecord,
    OutboxMessage,
)
from affiliation_management.domain.subscriptions.change import Movement
from affiliation_management.domain.subscriptions.model import (
    Periodicity,
    PlanVersionRef,
    Subscription,
    SubscriptionStatus,
)
from affiliation_management.infrastructure.persistence.models import (
    ACTIVE_SUBSCRIPTION_INDEX,
    INBOX_PRIMARY_KEY,
    InboxEventRow,
    OutboxEventRow,
    PlanOfferRow,
    PlanRow,
    PlanVersionRow,
    SubscriptionHistoryRow,
    SubscriptionRow,
)


def subscription_row(subscription: Subscription) -> SubscriptionRow:
    return SubscriptionRow(
        subscription_id=subscription.subscription_id,
        user_id=subscription.user_id,
        plan_code=subscription.plan.plan_code,
        plan_version=subscription.plan.version,
        status=subscription.status.value,
        origin=subscription.origin.value,
        started_at=subscription.started_at,
        version=subscription.version,
        created_at=subscription.started_at,
        updated_at=subscription.started_at,
        periodicity=subscription.periodicity.value,
        plan_offer_id=subscription.plan_offer_id,
        period_start=subscription.period_start,
        period_end=subscription.period_end,
    )


def movement_row(movement: Movement) -> SubscriptionHistoryRow:
    return SubscriptionHistoryRow(
        movement_id=movement.movement_id,
        user_id=movement.user_id,
        movement_type=movement.movement_type.value,
        occurred_at=movement.occurred_at,
        subscription_id=movement.subscription_id,
        commitment_id=movement.commitment_id,
        plan_code=movement.plan_code,
        periodicity=movement.periodicity.value,
    )


def outbox_row(message: OutboxMessage) -> OutboxEventRow:
    return OutboxEventRow(
        event_id=message.event_id,
        aggregate_type=message.aggregate_type,
        aggregate_id=message.aggregate_id,
        aggregate_version=message.aggregate_version,
        event_type=message.event_type,
        schema_version=message.schema_version,
        occurred_at=message.occurred_at,
        correlation_id=message.correlation_id,
        causation_id=message.causation_id,
        payload=message.payload,
        attempts=0,
        next_attempt_at=message.occurred_at,
    )


def _violated_constraint(error: IntegrityError) -> str | None:
    diagnostics = getattr(error.orig, "diag", None)
    return getattr(diagnostics, "constraint_name", None)


class SqlAlchemyProvisioningUnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None

    @property
    def session(self) -> Session:
        if self._session is None:
            raise RuntimeError("La unidad de trabajo no está abierta.")
        return self._session

    def __enter__(self) -> Self:
        self._session = self._session_factory()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # Todo lo no confirmado explícitamente se descarta.
        self.session.rollback()
        self.session.close()
        self._session = None

    def inbox_contains(self, event_id: UUID) -> bool:
        return self.session.get(InboxEventRow, event_id) is not None

    def has_active_subscription(self, user_id: UUID) -> bool:
        return bool(
            self.session.scalar(
                select(
                    exists().where(
                        SubscriptionRow.user_id == user_id,
                        SubscriptionRow.status == SubscriptionStatus.ACTIVE.value,
                    )
                )
            )
        )

    def default_athlete_plan(self, now: datetime) -> PlanVersionRef | None:
        row = self.session.execute(
            select(PlanVersionRow.plan_code, PlanVersionRow.version, PlanVersionRow.catalog_version)
            .join(PlanRow, PlanRow.code == PlanVersionRow.plan_code)
            .where(
                PlanRow.is_default_for_athlete.is_(True),
                PlanRow.status == "ACTIVE",
                PlanVersionRow.valid_from <= now,
                or_(PlanVersionRow.valid_to.is_(None), PlanVersionRow.valid_to > now),
            )
            .order_by(PlanVersionRow.version.desc())
            .limit(1)
        ).one_or_none()
        if row is None:
            return None
        return PlanVersionRef(plan_code=row[0], version=row[1], catalog_version=row[2])

    def none_offer_id(self, plan: PlanVersionRef) -> UUID | None:
        offer_id: UUID | None = self.session.scalar(
            select(PlanOfferRow.offer_id).where(
                PlanOfferRow.plan_code == plan.plan_code,
                PlanOfferRow.plan_version == plan.version,
                PlanOfferRow.periodicity == Periodicity.NONE.value,
            )
        )
        return offer_id

    def add_subscription(self, subscription: Subscription) -> None:
        # Se inserta ya: el historial la referencia por clave foránea y, sin `relationship`,
        # SQLAlchemy no ordena esas inserciones. Las unicidades se traducen igual que en commit.
        self.session.add(subscription_row(subscription))
        self._flush()

    def add_movement(self, movement: Movement) -> None:
        self.session.add(movement_row(movement))

    def add_outbox(self, message: OutboxMessage) -> None:
        self.session.add(outbox_row(message))

    def add_inbox(self, record: InboxRecord) -> None:
        self.session.add(
            InboxEventRow(
                event_id=record.event_id,
                event_type=record.event_type,
                user_id=record.user_id,
                result=record.result.value,
                processed_at=record.processed_at,
            )
        )

    def _flush(self) -> None:
        try:
            self.session.flush()
        except IntegrityError as error:
            self.session.rollback()
            self._translate(error)

    def commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            self._translate(error)

    @staticmethod
    def _translate(error: IntegrityError) -> None:
        constraint = _violated_constraint(error)
        if constraint == INBOX_PRIMARY_KEY:
            raise DuplicateDelivery() from None
        if constraint == ACTIVE_SUBSCRIPTION_INDEX:
            raise ActiveSubscriptionExists() from None
        raise error
