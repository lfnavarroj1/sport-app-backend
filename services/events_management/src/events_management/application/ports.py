"""Puertos que la aplicación necesita; la infraestructura los implementa."""

from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID

from events_management.domain.sport_events.model import SportEvent


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


class CapabilityChecker(Protocol):
    """Capacidades vigentes del deportista en Afiliación (ADR-024).

    Lanza `CapabilitiesUnavailable` si no puede evaluarlas.
    """

    def has_capability(self, user_id: UUID, capability: str) -> bool: ...


@dataclass(frozen=True)
class EventPosition:
    """Posición del cursor en el orden `starts_at`, `event_id`."""

    starts_at: datetime
    event_id: UUID


@dataclass(frozen=True)
class PublishedEventsQuery:
    starts_before_or_at: datetime  # `to` del periodo
    ends_at_or_after: datetime  # `from` del periodo
    after: EventPosition | None
    limit: int


@dataclass(frozen=True)
class CalendarEventView:
    """Evento publicado tal como lo muestra el calendario (sin `organizer_id` ni textos largos)."""

    event_id: UUID
    name: str
    discipline: str
    location_name: str
    location_city: str
    location_country_code: str
    starts_at: datetime
    ends_at: datetime
    time_zone: str
    status: str
    version: int


class PublishedEventReader(Protocol):
    def published(self, query: PublishedEventsQuery) -> list[CalendarEventView]:
        """Hasta `limit` eventos `PUBLISHED` que se superponen con el periodo, en orden
        `starts_at`, `event_id` y estrictamente después de `after`."""
        ...


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


@dataclass(frozen=True)
class IdempotencyRecord:
    """Resultado de un comando reintentable; la clave tiene alcance por actor (organizador)."""

    actor_id: UUID
    key: UUID
    operation: str
    request_fingerprint: str
    resource_id: UUID
    response: dict[str, Any]
    created_at: datetime
    expires_at: datetime


class Transaction(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class EventCreationUnitOfWork(Transaction, Protocol):
    """Transacción local de HU021: evento, idempotencia y outbox juntos."""

    def find_idempotency(self, actor_id: UUID, key: UUID) -> IdempotencyRecord | None: ...

    def delete_idempotency(self, actor_id: UUID, key: UUID) -> None: ...

    def add_event(self, event: SportEvent) -> None: ...

    def add_outbox(self, message: OutboxMessage) -> None: ...

    def add_idempotency(self, record: IdempotencyRecord) -> None: ...

    def commit(self) -> None:
        """Confirma todo o nada; traduce la clave duplicada a `IdempotencyKeyConflict`."""
        ...


class DuplicateDelivery(Exception):
    """Otra entrega del mismo evento confirmó primero su registro en `processed_event`."""


class ProjectionUnitOfWork(Transaction, Protocol):
    """Transacción local del proyector en la base de lectura."""

    def is_processed(self, event_id: UUID) -> bool: ...

    def projected_version(self, event_id: UUID) -> int | None:
        """Versión proyectada del evento; bloquea la fila para aplicar versiones en serie."""
        ...

    def add_event_view(self, event: SportEvent, projected_at: datetime) -> None: ...

    def add_processed(
        self, message_id: UUID, event_type: str, aggregate_id: UUID, processed_at: datetime
    ) -> None: ...

    def commit(self) -> None:
        """Traduce un mensaje o un evento ya proyectado por otra entrega a `DuplicateDelivery`."""
        ...
