"""Puertos que la aplicación necesita; la infraestructura los implementa."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID

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
from complementary_services.domain.catalog.publication import utc_instant


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
    return value.astimezone(UTC).isoformat()


def _optional_instant(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class ServiceView:
    """Servicio tal como se responde; instantes siempre en UTC.

    HU019 añade `published_at` (`None` en borrador); los documentos guardados por HU015
    antes de ese campo se leen como borradores sin publicación.
    """

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
            "published_at": _instant(offer.published_at) if offer.published_at else None,
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
                published_at=_optional_instant(document.get("published_at")),
            )
        )


@dataclass(frozen=True)
class PublicationView:
    """Respuesta de HU019 (ADR-027): estado de publicación vigente, instantes en UTC `Z`."""

    service_id: UUID
    provider_id: UUID
    status: ServiceStatus
    version: int
    published_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, offer: ServiceOffer) -> "PublicationView":
        if offer.published_at is None:
            raise ValueError("El servicio no está publicado.")
        return cls(
            service_id=offer.service_id,
            provider_id=offer.provider_id,
            status=offer.status,
            version=offer.version,
            published_at=offer.published_at,
            updated_at=offer.updated_at,
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "service_id": str(self.service_id),
            "provider_id": str(self.provider_id),
            "status": self.status.value,
            "version": self.version,
            "published_at": utc_instant(self.published_at),
            "updated_at": utc_instant(self.updated_at),
        }

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "PublicationView":
        return cls(
            service_id=UUID(document["service_id"]),
            provider_id=UUID(document["provider_id"]),
            status=ServiceStatus(document["status"]),
            version=int(document["version"]),
            published_at=datetime.fromisoformat(document["published_at"]),
            updated_at=datetime.fromisoformat(document["updated_at"]),
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


@dataclass(frozen=True)
class OutboxMessage:
    """Evento pendiente de publicar; se confirma en la misma transacción del agregado."""

    event_id: UUID
    event_type: str
    schema_version: int
    aggregate_type: str
    aggregate_id: UUID
    aggregate_version: int
    occurred_at: datetime
    correlation_id: UUID | None
    causation_id: str | None
    payload: dict[str, Any]


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


class PublicationUnitOfWork(Protocol):
    """Transacción local de HU019: servicio bloqueado, outbox e idempotencia juntos."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def find_idempotency(self, provider_id: UUID, key: UUID) -> IdempotencyRecord | None: ...

    def delete_idempotency(self, provider_id: UUID, key: UUID) -> None: ...

    def lock_service(self, service_id: UUID) -> ServiceOffer | None:
        """El servicio con su disponibilidad, bloqueado hasta el final de la transacción."""
        ...

    def save_publication(self, offer: ServiceOffer) -> None:
        """Estado, versión e instantes de la publicación del servicio bloqueado."""
        ...

    def add_outbox(self, message: OutboxMessage) -> None: ...

    def add_idempotency(self, record: IdempotencyRecord) -> None: ...

    def commit(self) -> None:
        """Confirma todo o nada; traduce la clave duplicada a `IdempotencyKeyConflict`."""
        ...


@dataclass(frozen=True)
class ServicePosition:
    """Posición keyset de «mis servicios»: `updated_at` y `service_id`, descendentes."""

    updated_at: datetime
    service_id: UUID


@dataclass(frozen=True)
class OwnServicesQuery:
    provider_id: UUID
    status: ServiceStatus | None
    after: ServicePosition | None
    limit: int


class ServiceReader(Protocol):
    """Consultas de los servicios propios del profesional (ADR-038) en la base única."""

    def list_own(self, query: OwnServicesQuery) -> list[ServiceOffer]:
        """Servicios del profesional ordenados por `updated_at` y `service_id` descendentes,
        estrictamente después de `after`, hasta `limit`."""
        ...

    def own_service(self, provider_id: UUID, service_id: UUID) -> ServiceOffer | None:
        """El servicio si existe y pertenece al profesional."""
        ...


class CapabilityChecker(Protocol):
    """HU020: capacidades vigentes del deportista según Afiliación (ADR-024, ADR-038).

    Lanza `CapabilitiesUnavailable` si no puede evaluarlas.
    """

    def has_capability(self, user_id: UUID, capability: str) -> bool: ...


@dataclass(frozen=True)
class CatalogPosition:
    """Posición keyset del catálogo de disponibilidad: `name` y `service_id`, ascendentes."""

    name: str
    service_id: UUID


@dataclass(frozen=True)
class AvailabilityQuery:
    """Servicios `PUBLISHED` con los intervalos que empiezan en `[starts_from, starts_before)`
    y después de `now`. `categories`/`modalities` en `None` no filtran."""

    starts_from: datetime
    starts_before: datetime
    now: datetime
    categories: frozenset[Category] | None
    modalities: frozenset[Modality] | None
    only_available: bool
    after: CatalogPosition | None
    limit: int


class ServiceCatalogReader(Protocol):
    """Catálogo de servicios publicados para el deportista (HU020) en la base única."""

    def published_availability(self, query: AvailabilityQuery) -> list[ServiceOffer]:
        """Servicios ordenados por `name` y `service_id`, estrictamente después de `after`,
        hasta `limit`; cada oferta trae solo sus intervalos dentro del rango."""
        ...


@dataclass(frozen=True)
class AssociatedService:
    """Resumen del servicio asociado (ADR-038): sin datos del profesional ni de salud."""

    name: str
    category: Category
    modality: Modality
    fee: Price

    @classmethod
    def of(cls, offer: ServiceOffer) -> "AssociatedService":
        return cls(
            name=offer.name, category=offer.category, modality=offer.modality, fee=offer.price
        )


@dataclass(frozen=True)
class AssociationView:
    """Asociación tal como se responde (HU017); instantes en UTC `Z`."""

    association: ServiceAssociation
    service: AssociatedService

    def to_document(self) -> dict[str, Any]:
        association, service = self.association, self.service
        return {
            "association_id": str(association.association_id),
            "service_id": str(association.service_id),
            "service": {
                "name": service.name,
                "category": service.category.value,
                "modality": service.modality.value,
                "fee": {
                    "amount": str(service.fee.amount),
                    "currency": service.fee.currency.value,
                    "billing_unit": service.fee.billing_unit.value,
                },
            },
            "status": association.status.value,
            "associated_at": utc_instant(association.associated_at),
            "ended_at": utc_instant(association.ended_at) if association.ended_at else None,
            "version": association.version,
        }

    @classmethod
    def from_document(cls, document: dict[str, Any], athlete_id: UUID) -> "AssociationView":
        """`athlete_id` no se responde: es el del token que guardó el resultado."""
        service, fee = document["service"], document["service"]["fee"]
        return cls(
            association=ServiceAssociation(
                association_id=UUID(document["association_id"]),
                service_id=UUID(document["service_id"]),
                athlete_id=athlete_id,
                status=AssociationStatus(document["status"]),
                associated_at=datetime.fromisoformat(document["associated_at"]),
                version=int(document["version"]),
                ended_at=_optional_instant(document.get("ended_at")),
            ),
            service=AssociatedService(
                name=service["name"],
                category=Category(service["category"]),
                modality=Modality(service["modality"]),
                fee=Price(
                    amount=Decimal(fee["amount"]),
                    currency=Currency(fee["currency"]),
                    billing_unit=BillingUnit(fee["billing_unit"]),
                ),
            ),
        )


class AssociationUnitOfWork(Protocol):
    """Transacción local de HU017: asociación e idempotencia del deportista juntas.

    Reutiliza la tabla de idempotencia de HU015/HU019, con la clave por usuario autenticado.
    """

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def find_idempotency(self, provider_id: UUID, key: UUID) -> IdempotencyRecord | None: ...

    def delete_idempotency(self, provider_id: UUID, key: UUID) -> None: ...

    def add_idempotency(self, record: IdempotencyRecord) -> None: ...

    def service(self, service_id: UUID) -> ServiceOffer | None:
        """El servicio sin su disponibilidad (la asociación no la usa)."""
        ...

    def active_association(
        self, athlete_id: UUID, service_id: UUID
    ) -> ServiceAssociation | None: ...

    def add_association(self, association: ServiceAssociation) -> None: ...

    def lock_association(self, association_id: UUID) -> ServiceAssociation | None:
        """La asociación bloqueada hasta el final de la transacción."""
        ...

    def save_end(self, association: ServiceAssociation) -> None:
        """Estado, `ended_at` y versión de la asociación bloqueada."""
        ...

    def commit(self) -> None:
        """Confirma todo o nada; traduce la clave duplicada a `IdempotencyKeyConflict` y la
        asociación activa duplicada a `ActiveAssociationConflict`."""
        ...


@dataclass(frozen=True)
class AssociationPosition:
    """Posición keyset de «mis asociaciones»: `associated_at` y `association_id`,
    descendentes."""

    associated_at: datetime
    association_id: UUID


@dataclass(frozen=True)
class OwnAssociationsQuery:
    """`status` en `None` incluye activas e inactivas (`ALL`)."""

    athlete_id: UUID
    status: AssociationStatus | None
    after: AssociationPosition | None
    limit: int


class AssociationReader(Protocol):
    """Asociaciones propias del deportista con el resumen del servicio (HU017)."""

    def list_own(self, query: OwnAssociationsQuery) -> list[AssociationView]:
        """Ordenadas por `associated_at` y `association_id` descendentes, estrictamente
        después de `after`, hasta `limit`."""
        ...
