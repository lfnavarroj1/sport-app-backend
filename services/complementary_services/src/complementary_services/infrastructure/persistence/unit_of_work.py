"""Unidades de trabajo SQLAlchemy de la base única de servicios (HU015, HU019, HU017)."""

from collections.abc import Iterable
from datetime import UTC
from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from complementary_services.application.errors import (
    ActiveAssociationConflict,
    IdempotencyKeyConflict,
)
from complementary_services.application.ports import IdempotencyRecord, OutboxMessage
from complementary_services.domain.association.association import (
    AssociationStatus,
    ServiceAssociation,
)
from complementary_services.domain.availability.intervals import AvailabilityInterval
from complementary_services.domain.catalog.offer import (
    BillingUnit,
    Category,
    Currency,
    Modality,
    Price,
    ServiceOffer,
    ServiceStatus,
)
from complementary_services.infrastructure.persistence.models import (
    ACTIVE_ASSOCIATION_INDEX,
    IDEMPOTENCY_PRIMARY_KEY,
    IdempotencyKeyRow,
    OutboxEventRow,
    ServiceAssociationRow,
    ServiceAvailabilityRow,
    ServiceRow,
)


def _violated_constraint(error: IntegrityError) -> str | None:
    diagnostics = getattr(error.orig, "diag", None)
    return getattr(diagnostics, "constraint_name", None)


def service_offer(row: ServiceRow, intervals: Iterable[ServiceAvailabilityRow]) -> ServiceOffer:
    """Agregado a partir de sus filas; los instantes se devuelven siempre en UTC."""
    return ServiceOffer(
        service_id=row.service_id,
        provider_id=row.provider_id,
        name=row.name,
        description=row.description,
        category=Category(row.category),
        modality=Modality(row.modality),
        price=Price(
            amount=row.price_amount,
            currency=Currency(row.currency),
            billing_unit=BillingUnit(row.billing_unit),
        ),
        availability=tuple(
            AvailabilityInterval(
                starts_at=interval.starts_at.astimezone(UTC),
                ends_at=interval.ends_at.astimezone(UTC),
                time_zone=interval.time_zone,
                capacity=interval.capacity,
            )
            for interval in sorted(intervals, key=lambda item: item.position)
        ),
        status=ServiceStatus(row.status),
        version=row.version,
        created_at=row.created_at.astimezone(UTC),
        updated_at=row.updated_at.astimezone(UTC),
        published_at=row.published_at.astimezone(UTC) if row.published_at else None,
    )


class _UnitOfWork:
    """Sesión e idempotencia por profesional comunes a los comandos."""

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

    def find_idempotency(self, provider_id: UUID, key: UUID) -> IdempotencyRecord | None:
        # Sin autoflush: los conflictos deben aflorar en `commit`, donde se traducen.
        with self.session.no_autoflush:
            row = self.session.get(IdempotencyKeyRow, (provider_id, key))
        if row is None:
            return None
        return IdempotencyRecord(
            provider_id=row.provider_id,
            key=row.key,
            operation=row.operation,
            request_fingerprint=row.request_fingerprint,
            resource_id=row.resource_id,
            response=row.response,
            created_at=row.created_at,
            expires_at=row.expires_at,
        )

    def delete_idempotency(self, provider_id: UUID, key: UUID) -> None:
        self.session.execute(
            delete(IdempotencyKeyRow).where(
                IdempotencyKeyRow.provider_id == provider_id, IdempotencyKeyRow.key == key
            )
        )

    def add_idempotency(self, record: IdempotencyRecord) -> None:
        self.session.add(
            IdempotencyKeyRow(
                provider_id=record.provider_id,
                key=record.key,
                operation=record.operation,
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
            if _violated_constraint(error) == IDEMPOTENCY_PRIMARY_KEY:
                raise IdempotencyKeyConflict() from None
            raise


class SqlAlchemyCreationUnitOfWork(_UnitOfWork):
    """HU015: servicio en borrador, disponibilidad e idempotencia."""

    def add_service(self, offer: ServiceOffer) -> None:
        self.session.add(
            ServiceRow(
                service_id=offer.service_id,
                provider_id=offer.provider_id,
                name=offer.name,
                description=offer.description,
                category=offer.category.value,
                modality=offer.modality.value,
                price_amount=offer.price.amount,
                currency=offer.price.currency.value,
                billing_unit=offer.price.billing_unit.value,
                status=offer.status.value,
                version=offer.version,
                created_at=offer.created_at,
                updated_at=offer.updated_at,
                published_at=offer.published_at,
            )
        )
        # El servicio debe existir antes que sus intervalos (clave foránea local).
        self.session.flush()
        self.session.add_all(
            ServiceAvailabilityRow(
                service_id=offer.service_id,
                position=position,
                starts_at=interval.starts_at,
                ends_at=interval.ends_at,
                time_zone=interval.time_zone,
                capacity=interval.capacity,
            )
            for position, interval in enumerate(offer.availability)
        )


class SqlAlchemyPublicationUnitOfWork(_UnitOfWork):
    """HU019: servicio bloqueado, transición, `servicio.publicado.v1` e idempotencia."""

    def lock_service(self, service_id: UUID) -> ServiceOffer | None:
        row = self.session.scalar(
            select(ServiceRow).where(ServiceRow.service_id == service_id).with_for_update()
        )
        if row is None:
            return None
        intervals = self.session.scalars(
            select(ServiceAvailabilityRow).where(ServiceAvailabilityRow.service_id == service_id)
        )
        return service_offer(row, intervals)

    def save_publication(self, offer: ServiceOffer) -> None:
        # La fila está bloqueada; la versión anterior se exige igualmente como defensa.
        result = self.session.execute(
            update(ServiceRow)
            .where(
                ServiceRow.service_id == offer.service_id,
                ServiceRow.version == offer.version - 1,
            )
            .values(
                status=offer.status.value,
                version=offer.version,
                updated_at=offer.updated_at,
                published_at=offer.published_at,
            )
            .execution_options(synchronize_session=False)
        )
        if getattr(result, "rowcount", 0) != 1:
            raise RuntimeError("El servicio bloqueado cambió de versión.")

    def add_outbox(self, message: OutboxMessage) -> None:
        self.session.add(
            OutboxEventRow(
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
        )


def service_association(row: ServiceAssociationRow) -> ServiceAssociation:
    """Agregado a partir de su fila; los instantes se devuelven siempre en UTC."""
    return ServiceAssociation(
        association_id=row.association_id,
        service_id=row.service_id,
        athlete_id=row.athlete_id,
        status=AssociationStatus(row.status),
        associated_at=row.associated_at.astimezone(UTC),
        version=row.version,
        ended_at=row.ended_at.astimezone(UTC) if row.ended_at else None,
    )


class SqlAlchemyAssociationUnitOfWork(_UnitOfWork):
    """HU017: asociación del deportista e idempotencia; sin outbox (no hay eventos)."""

    def service(self, service_id: UUID) -> ServiceOffer | None:
        row = self.session.get(ServiceRow, service_id)
        return service_offer(row, ()) if row is not None else None

    def active_association(self, athlete_id: UUID, service_id: UUID) -> ServiceAssociation | None:
        with self.session.no_autoflush:
            row = self.session.scalar(
                select(ServiceAssociationRow).where(
                    ServiceAssociationRow.athlete_id == athlete_id,
                    ServiceAssociationRow.service_id == service_id,
                    ServiceAssociationRow.status == AssociationStatus.ACTIVE.value,
                )
            )
        return service_association(row) if row is not None else None

    def add_association(self, association: ServiceAssociation) -> None:
        self.session.add(
            ServiceAssociationRow(
                association_id=association.association_id,
                service_id=association.service_id,
                athlete_id=association.athlete_id,
                status=association.status.value,
                associated_at=association.associated_at,
                ended_at=association.ended_at,
                version=association.version,
            )
        )

    def lock_association(self, association_id: UUID) -> ServiceAssociation | None:
        row = self.session.scalar(
            select(ServiceAssociationRow)
            .where(ServiceAssociationRow.association_id == association_id)
            .with_for_update()
        )
        return service_association(row) if row is not None else None

    def save_end(self, association: ServiceAssociation) -> None:
        # La fila está bloqueada; la versión anterior se exige igualmente como defensa.
        result = self.session.execute(
            update(ServiceAssociationRow)
            .where(
                ServiceAssociationRow.association_id == association.association_id,
                ServiceAssociationRow.version == association.version - 1,
            )
            .values(
                status=association.status.value,
                ended_at=association.ended_at,
                version=association.version,
            )
            .execution_options(synchronize_session=False)
        )
        if getattr(result, "rowcount", 0) != 1:
            raise RuntimeError("La asociación bloqueada cambió de versión.")

    def commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            constraint = _violated_constraint(error)
            if constraint == IDEMPOTENCY_PRIMARY_KEY:
                raise IdempotencyKeyConflict() from None
            if constraint == ACTIVE_ASSOCIATION_INDEX:
                raise ActiveAssociationConflict() from None
            raise
