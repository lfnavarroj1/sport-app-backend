"""Puertos que la aplicación necesita; la infraestructura los implementa."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID

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


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class Caller:
    """Identidad verificada a partir del access token; nunca del cuerpo de la solicitud."""

    user_id: UUID
    actor_type: str


class PermissionPolicy(Protocol):
    """Contrato versionado `actor-permissions.v1` (ADR-025)."""

    def allows(self, actor_type: str, permission: str) -> bool: ...


def _instant(value: datetime) -> str:
    return value.isoformat()


@dataclass(frozen=True)
class ServiceView:
    """Servicio tal como se responde; instantes siempre en UTC."""

    offer: ServiceOffer

    def to_document(self) -> dict[str, Any]:
        offer = self.offer
        return {
            "service_id": str(offer.service_id),
            "provider_id": str(offer.provider_id),
            "name": offer.name,
            "description": offer.description,
            "category": offer.category.value,
            "modality": offer.modality.value,
            "price": {
                "amount": str(offer.price.amount),
                "currency": offer.price.currency.value,
                "billing_unit": offer.price.billing_unit.value,
            },
            "availability": [
                {
                    "starts_at": _instant(interval.starts_at),
                    "ends_at": _instant(interval.ends_at),
                    "time_zone": interval.time_zone,
                    "capacity": interval.capacity,
                }
                for interval in offer.availability
            ],
            "status": offer.status.value,
            "version": offer.version,
            "created_at": _instant(offer.created_at),
            "updated_at": _instant(offer.updated_at),
        }

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "ServiceView":
        price = document["price"]
        return cls(
            ServiceOffer(
                service_id=UUID(document["service_id"]),
                provider_id=UUID(document["provider_id"]),
                name=document["name"],
                description=document["description"],
                category=Category(document["category"]),
                modality=Modality(document["modality"]),
                price=Price(
                    amount=Decimal(price["amount"]),
                    currency=Currency(price["currency"]),
                    billing_unit=BillingUnit(price["billing_unit"]),
                ),
                availability=tuple(
                    AvailabilityInterval(
                        starts_at=datetime.fromisoformat(item["starts_at"]),
                        ends_at=datetime.fromisoformat(item["ends_at"]),
                        time_zone=item["time_zone"],
                        capacity=int(item["capacity"]),
                    )
                    for item in document["availability"]
                ),
                status=ServiceStatus(document["status"]),
                version=int(document["version"]),
                created_at=datetime.fromisoformat(document["created_at"]),
                updated_at=datetime.fromisoformat(document["updated_at"]),
            )
        )


@dataclass(frozen=True)
class IdempotencyRecord:
    provider_id: UUID
    key: UUID
    operation: str
    request_fingerprint: str
    resource_id: UUID
    response: dict[str, Any]
    created_at: datetime
    expires_at: datetime


class CreationUnitOfWork(Protocol):
    """Transacción local de HU015: servicio, disponibilidad e idempotencia juntos."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def find_idempotency(self, provider_id: UUID, key: UUID) -> IdempotencyRecord | None: ...

    def delete_idempotency(self, provider_id: UUID, key: UUID) -> None: ...

    def add_service(self, offer: ServiceOffer) -> None: ...

    def add_idempotency(self, record: IdempotencyRecord) -> None: ...

    def commit(self) -> None:
        """Confirma todo o nada; traduce la clave duplicada a `IdempotencyKeyConflict`."""
        ...
