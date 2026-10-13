"""Unidad de trabajo SQLAlchemy de la creación de servicios (HU015)."""

from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from complementary_services.application.errors import IdempotencyKeyConflict
from complementary_services.application.ports import IdempotencyRecord
from complementary_services.domain.catalog.offer import ServiceOffer
from complementary_services.infrastructure.persistence.models import (
    IDEMPOTENCY_PRIMARY_KEY,
    IdempotencyKeyRow,
    ServiceAvailabilityRow,
    ServiceRow,
)


def _violated_constraint(error: IntegrityError) -> str | None:
    diagnostics = getattr(error.orig, "diag", None)
    return getattr(diagnostics, "constraint_name", None)


class SqlAlchemyCreationUnitOfWork:
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
