"""HU016 (ADR-036): catálogo vigente, suscripción del deportista y su cambio en PostgreSQL.

El cambio confirma en una transacción el cierre de la suscripción vigente, la nueva, los
compromisos, el historial, la outbox y la clave de idempotencia. La suscripción activa se
bloquea (`FOR UPDATE`) para que un solo cambio concurrente gane.
"""

from datetime import datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from sqlalchemy import Select, delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from affiliation_management.application.errors import IdempotencyKeyConflict
from affiliation_management.application.ports import (
    ActiveSubscriptionExists,
    IdempotencyRecord,
    OfferView,
    OutboxMessage,
    PlanView,
    SubscriptionView,
)
from affiliation_management.domain.subscriptions.change import (
    BillingCommitment,
    CatalogPlan,
    CommitmentStatus,
    Movement,
    PlanOffer,
)
from affiliation_management.domain.subscriptions.model import (
    Periodicity,
    PlanVersionRef,
    Subscription,
    SubscriptionOrigin,
    SubscriptionStatus,
)
from affiliation_management.infrastructure.persistence.models import (
    ACTIVE_SUBSCRIPTION_INDEX,
    COMMAND_IDEMPOTENCY_PRIMARY_KEY,
    BillingCommitmentRow,
    CommandIdempotencyRow,
    PlanOfferRow,
    PlanRow,
    PlanVersionCapabilityRow,
    PlanVersionRow,
    SubscriptionRow,
)
from affiliation_management.infrastructure.persistence.unit_of_work import (
    movement_row,
    outbox_row,
    subscription_row,
)


def _violated_constraint(error: IntegrityError) -> str | None:
    diagnostics = getattr(error.orig, "diag", None)
    return getattr(diagnostics, "constraint_name", None)


def _valid_at(now: datetime) -> Any:
    return (PlanVersionRow.valid_from <= now) & or_(
        PlanVersionRow.valid_to.is_(None), PlanVersionRow.valid_to > now
    )


def _active_query(user_id: UUID) -> Select[SubscriptionRow, str]:
    return (
        select(SubscriptionRow, PlanVersionRow.catalog_version)
        .join(
            PlanVersionRow,
            (PlanVersionRow.plan_code == SubscriptionRow.plan_code)
            & (PlanVersionRow.version == SubscriptionRow.plan_version),
        )
        .where(
            SubscriptionRow.user_id == user_id,
            SubscriptionRow.status == SubscriptionStatus.ACTIVE.value,
        )
    )


def _subscription(row: SubscriptionRow, catalog_version: str) -> Subscription:
    return Subscription(
        subscription_id=row.subscription_id,
        user_id=row.user_id,
        plan=PlanVersionRef(
            plan_code=row.plan_code, version=row.plan_version, catalog_version=catalog_version
        ),
        status=SubscriptionStatus(row.status),
        origin=SubscriptionOrigin(row.origin),
        started_at=row.started_at,
        version=row.version,
        periodicity=Periodicity(row.periodicity or Periodicity.NONE.value),
        plan_offer_id=row.plan_offer_id,
        period_start=row.period_start or row.started_at,
        period_end=row.period_end,
    )


def _commitment(row: BillingCommitmentRow) -> BillingCommitment:
    return BillingCommitment(
        commitment_id=row.commitment_id,
        subscription_id=row.subscription_id,
        user_id=row.user_id,
        plan_code=row.plan_code,
        plan_version=row.plan_version,
        plan_offer_id=row.plan_offer_id,
        periodicity=Periodicity(row.periodicity),
        amount=row.amount,
        currency=row.currency,
        period_start=row.period_start,
        period_end=row.period_end,
        due_at=row.due_at,
        status=CommitmentStatus(row.status),
        created_at=row.created_at,
    )


class SqlAlchemyCatalogReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def current_plan(self, plan_code: str, now: datetime) -> CatalogPlan | None:
        with self._session_factory() as session:
            plan = session.get(PlanRow, plan_code)
            if plan is None:
                return None
            version = session.execute(
                select(PlanVersionRow.version, PlanVersionRow.catalog_version)
                .where(PlanVersionRow.plan_code == plan_code, _valid_at(now))
                .order_by(PlanVersionRow.version.desc())
                .limit(1)
            ).one_or_none()
            if version is None:
                return None
            ref = PlanVersionRef(
                plan_code=plan_code, version=version[0], catalog_version=version[1]
            )
            offers = session.scalars(
                select(PlanOfferRow).where(
                    PlanOfferRow.plan_code == plan_code,
                    PlanOfferRow.plan_version == ref.version,
                    PlanOfferRow.valid_from <= now,
                    or_(PlanOfferRow.valid_to.is_(None), PlanOfferRow.valid_to > now),
                )
            ).all()
            selectable = plan.is_selectable and plan.status == "ACTIVE"
            return CatalogPlan(
                plan_code=plan_code,
                selectable=selectable,
                offers=tuple(
                    PlanOffer(
                        offer_id=offer.offer_id,
                        plan=ref,
                        periodicity=Periodicity(offer.periodicity),
                        price=offer.price,
                        currency=offer.currency,
                        duration_months=offer.duration_months,
                        selectable=selectable,
                    )
                    for offer in offers
                ),
            )

    def catalog(self, catalog_version: str) -> list[PlanView]:
        with self._session_factory() as session:
            rows = session.execute(
                select(PlanRow, PlanVersionRow.version)
                .join(PlanVersionRow, PlanVersionRow.plan_code == PlanRow.code)
                .where(PlanVersionRow.catalog_version == catalog_version)
                .order_by(PlanRow.display_order)
            ).all()
            views: list[PlanView] = []
            for plan, version in rows:
                capabilities = session.scalars(
                    select(PlanVersionCapabilityRow.capability_code)
                    .where(
                        PlanVersionCapabilityRow.plan_code == plan.code,
                        PlanVersionCapabilityRow.plan_version == version,
                    )
                    .order_by(PlanVersionCapabilityRow.capability_code)
                ).all()
                offers = session.scalars(
                    select(PlanOfferRow)
                    .where(
                        PlanOfferRow.plan_code == plan.code, PlanOfferRow.plan_version == version
                    )
                    .order_by(PlanOfferRow.duration_months.nulls_first())
                ).all()
                views.append(
                    PlanView(
                        plan_code=plan.code,
                        name=plan.name,
                        description=plan.description,
                        level=plan.level,
                        version=version,
                        selectable=plan.is_selectable and plan.status == "ACTIVE",
                        capabilities=tuple(capabilities),
                        offers=tuple(
                            OfferView(
                                periodicity=offer.periodicity,
                                price=offer.price,
                                currency=offer.currency,
                                duration_months=offer.duration_months,
                            )
                            for offer in offers
                        ),
                    )
                )
        return views


class SqlAlchemySubscriptionReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def active(self, user_id: UUID) -> SubscriptionView | None:
        with self._session_factory() as session:
            return active_view(session, user_id)


def active_view(session: Session, user_id: UUID) -> SubscriptionView | None:
    """Suscripción `ACTIVE` del usuario con el precio de su oferta (también la usa HU007)."""
    found = session.execute(_active_query(user_id)).one_or_none()
    if found is None:
        return None
    row, catalog_version = found
    price = (
        session.scalar(select(PlanOfferRow.price).where(PlanOfferRow.offer_id == row.plan_offer_id))
        if row.plan_offer_id is not None
        else None
    )
    return SubscriptionView(subscription=_subscription(row, catalog_version), price=price)


class SqlAlchemySubscriptionChangeUnitOfWork:
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

    def find_idempotency(
        self, actor_id: UUID, operation: str, key: UUID
    ) -> IdempotencyRecord | None:
        # Sin *autoflush*: la consulta no debe adelantar un conflicto que solo se traduce
        # en `commit` (lección de HU032).
        with self.session.no_autoflush:
            row = self.session.scalar(
                select(CommandIdempotencyRow)
                .where(
                    CommandIdempotencyRow.actor_id == actor_id,
                    CommandIdempotencyRow.operation == operation,
                    CommandIdempotencyRow.key == key,
                )
                .execution_options(populate_existing=True)
            )
        if row is None:
            return None
        return IdempotencyRecord(
            actor_id=row.actor_id,
            operation=row.operation,
            key=row.key,
            request_fingerprint=row.request_fingerprint,
            resource_id=row.resource_id,
            response=row.response,
            created_at=row.created_at,
            expires_at=row.expires_at,
        )

    def delete_idempotency(self, actor_id: UUID, operation: str, key: UUID) -> None:
        self.session.execute(
            delete(CommandIdempotencyRow).where(
                CommandIdempotencyRow.actor_id == actor_id,
                CommandIdempotencyRow.operation == operation,
                CommandIdempotencyRow.key == key,
            )
        )

    def lock_active_subscription(self, user_id: UUID) -> Subscription | None:
        found = self.session.execute(
            _active_query(user_id)
            .with_for_update(of=SubscriptionRow)
            .execution_options(populate_existing=True)
        ).one_or_none()
        return _subscription(found[0], found[1]) if found is not None else None

    def active_subscription(self, user_id: UUID) -> Subscription | None:
        found = self.session.execute(
            _active_query(user_id).execution_options(populate_existing=True)
        ).one_or_none()
        return _subscription(found[0], found[1]) if found is not None else None

    def pending_commitments(self, user_id: UUID) -> list[BillingCommitment]:
        rows = self.session.scalars(
            select(BillingCommitmentRow)
            .where(
                BillingCommitmentRow.user_id == user_id,
                BillingCommitmentRow.status == CommitmentStatus.PENDING_EXTERNAL.value,
            )
            .order_by(BillingCommitmentRow.created_at)
            .with_for_update()
        ).all()
        return [_commitment(row) for row in rows]

    def end_subscription(self, subscription: Subscription, ended_at: datetime) -> None:
        # Se aplica de inmediato: la fila deja de estar `ACTIVE` antes de insertar la nueva
        # (índice único parcial de una suscripción activa por usuario).
        self.session.execute(
            update(SubscriptionRow)
            .where(SubscriptionRow.subscription_id == subscription.subscription_id)
            .values(status=SubscriptionStatus.ENDED.value, ended_at=ended_at, updated_at=ended_at),
            execution_options={"synchronize_session": False},
        )

    def add_subscription(self, subscription: Subscription) -> None:
        # Se inserta ya: compromisos e historial la referencian por clave foránea y, sin
        # `relationship`, SQLAlchemy no ordena esas inserciones.
        self.session.add(subscription_row(subscription))
        try:
            self.session.flush()
        except IntegrityError as error:
            self.session.rollback()
            self._translate(error)

    def add_commitment(self, commitment: BillingCommitment) -> None:
        # Mismo motivo: el movimiento `COMMITMENT_CREATED` lo referencia.
        self.session.add(
            BillingCommitmentRow(
                commitment_id=commitment.commitment_id,
                subscription_id=commitment.subscription_id,
                user_id=commitment.user_id,
                plan_code=commitment.plan_code,
                plan_version=commitment.plan_version,
                plan_offer_id=commitment.plan_offer_id,
                periodicity=commitment.periodicity.value,
                amount=commitment.amount,
                currency=commitment.currency,
                period_start=commitment.period_start,
                period_end=commitment.period_end,
                due_at=commitment.due_at,
                status=commitment.status.value,
                waived_at=None,
                created_at=commitment.created_at,
            )
        )
        self.session.flush()

    def waive_commitment(self, commitment_id: UUID, waived_at: datetime) -> None:
        self.session.execute(
            update(BillingCommitmentRow)
            .where(BillingCommitmentRow.commitment_id == commitment_id)
            .values(status=CommitmentStatus.WAIVED.value, waived_at=waived_at),
            execution_options={"synchronize_session": False},
        )

    def add_movement(self, movement: Movement) -> None:
        self.session.add(movement_row(movement))

    def add_outbox(self, message: OutboxMessage) -> None:
        self.session.add(outbox_row(message))

    def add_idempotency(self, record: IdempotencyRecord) -> None:
        self.session.add(
            CommandIdempotencyRow(
                actor_id=record.actor_id,
                operation=record.operation,
                key=record.key,
                request_fingerprint=record.request_fingerprint,
                resource_id=record.resource_id,
                response=record.response,
                created_at=record.created_at,
                expires_at=record.expires_at,
            )
        )

    def commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            self._translate(error)

    @staticmethod
    def _translate(error: IntegrityError) -> None:
        constraint = _violated_constraint(error)
        if constraint == COMMAND_IDEMPOTENCY_PRIMARY_KEY:
            raise IdempotencyKeyConflict() from None
        if constraint == ACTIVE_SUBSCRIPTION_INDEX:
            raise ActiveSubscriptionExists() from None
        raise error
