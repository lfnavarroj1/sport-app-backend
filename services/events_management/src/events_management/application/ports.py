"""Puertos que la aplicación necesita; la infraestructura los implementa."""

from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID

from events_management.domain.registrations.model import (
    EventForRegistration,
    EventRegistration,
)
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
    # HU023 (ADR-040): términos ya normalizados (`normalize_term`); `None` = sin filtro.
    discipline: str | None = None
    city: str | None = None


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


@dataclass(frozen=True)
class OwnEventsQuery:
    """HU024: eventos del organizador autenticado, opcionalmente de un estado."""

    organizer_id: UUID
    status: str | None
    after: EventPosition | None
    limit: int


class OwnEventReader(Protocol):
    def list(self, query: OwnEventsQuery) -> list[SportEvent]:
        """Hasta `limit` eventos propios en orden `starts_at`, `event_id` y estrictamente
        después de `after`."""
        ...


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

    def find_idempotency(
        self, actor_id: UUID, operation: str, key: UUID
    ) -> IdempotencyRecord | None: ...

    def delete_idempotency(self, actor_id: UUID, operation: str, key: UUID) -> None: ...

    def add_event(self, event: SportEvent) -> None: ...

    def add_outbox(self, message: OutboxMessage) -> None: ...

    def add_idempotency(self, record: IdempotencyRecord) -> None: ...

    def commit(self) -> None:
        """Confirma todo o nada; traduce la clave duplicada a `IdempotencyKeyConflict`."""
        ...


class PublicationUnitOfWork(Transaction, Protocol):
    """Transacción local de HU024: evento bloqueado, transición, idempotencia y outbox."""

    def find_idempotency(
        self, actor_id: UUID, operation: str, key: UUID
    ) -> IdempotencyRecord | None: ...

    def delete_idempotency(self, actor_id: UUID, operation: str, key: UUID) -> None: ...

    def lock_event(self, event_id: UUID) -> SportEvent | None:
        """Bloquea la fila del evento hasta el fin de la transacción: serializa las
        publicaciones concurrentes."""
        ...

    def update_event(self, event: SportEvent) -> None:
        """Guarda estado, versión, `updated_at` y `published_at` del evento bloqueado."""
        ...

    def add_outbox(self, message: OutboxMessage) -> None: ...

    def add_idempotency(self, record: IdempotencyRecord) -> None: ...

    def commit(self) -> None:
        """Traduce la clave duplicada a `IdempotencyKeyConflict`."""
        ...


class RegistrationUnitOfWork(Transaction, Protocol):
    """Transacción local de HU041: evento bloqueado, inscripción, idempotencia y outbox."""

    def find_idempotency(
        self, actor_id: UUID, operation: str, key: UUID
    ) -> IdempotencyRecord | None: ...

    def delete_idempotency(self, actor_id: UUID, operation: str, key: UUID) -> None: ...

    def lock_event(self, event_id: UUID) -> EventForRegistration | None:
        """Bloquea la fila del evento hasta el fin de la transacción: serializa el cupo."""
        ...

    def event_summary(self, event_id: UUID) -> "EventSummary":
        """Datos del evento (ya bloqueado) que acompañan a la inscripción en la respuesta."""
        ...

    def count_registrations(self, event_id: UUID) -> int: ...

    def find_registration(self, event_id: UUID, athlete_id: UUID) -> EventRegistration | None: ...

    def add_registration(self, registration: EventRegistration) -> None: ...

    def add_outbox(self, message: OutboxMessage) -> None: ...

    def add_idempotency(self, record: IdempotencyRecord) -> None: ...

    def commit(self) -> None:
        """Traduce la clave duplicada a `IdempotencyKeyConflict` y una segunda inscripción del
        mismo deportista al evento a `RegistrationConflict`."""
        ...


@dataclass(frozen=True)
class EventSummary:
    """Datos del evento que acompañan a una inscripción en las respuestas."""

    name: str
    starts_at: datetime
    ends_at: datetime
    time_zone: str
    location_name: str
    location_city: str
    location_country_code: str


@dataclass(frozen=True)
class RegistrationView:
    registration: EventRegistration
    event: EventSummary


@dataclass(frozen=True)
class EventDetailView:
    """Evento publicado tal como lo ve el deportista (HU041), sin `organizer_id`."""

    event: CalendarEventView
    description: str
    capacity: int
    my_registration: EventRegistration | None


@dataclass(frozen=True)
class RegistrationPosition:
    """Posición del cursor en el orden `event.starts_at`, `registration_id`."""

    starts_at: datetime
    registration_id: UUID


@dataclass(frozen=True)
class RegistrationsQuery:
    athlete_id: UUID
    status: str | None
    after: RegistrationPosition | None
    limit: int


class EventDetailReader(Protocol):
    def visible_detail(
        self, event_id: UUID, *, athlete_id: UUID | None, owner_id: UUID | None
    ) -> EventDetailView | None:
        """El evento si es visible (ADR-033): `PUBLISHED` para cualquiera; `DRAFT` solo si su
        organizador es `owner_id`. Con `athlete_id`, la inscripción proyectada de ese
        deportista; sin él, `my_registration` es `None`."""
        ...


class RegistrationReader(Protocol):
    def list(self, query: RegistrationsQuery) -> list[RegistrationView]:
        """Hasta `limit` inscripciones propias, en orden ascendente estrictamente después de
        `after`."""
        ...


class DuplicateDelivery(Exception):
    """Otra entrega del mismo evento confirmó primero su registro en `processed_event`."""


class ConcurrentEventView(Exception):
    """Otro mensaje del mismo evento creó primero su vista; hay que reaplicar por versión."""


class ProjectionUnitOfWork(Transaction, Protocol):
    """Transacción local del proyector en la base de lectura."""

    def is_processed(self, event_id: UUID) -> bool: ...

    def projected_version(self, event_id: UUID) -> int | None:
        """Versión proyectada del evento; bloquea la fila para aplicar versiones en serie."""
        ...

    def add_event_view(self, event: SportEvent, projected_at: datetime) -> None: ...

    def replace_event_view(self, event: SportEvent, projected_at: datetime) -> None:
        """Estado completo (ADR-033): sustituye todas las columnas de la vista existente."""
        ...

    def registration_version(self, registration_id: UUID) -> int | None:
        """Versión proyectada de la inscripción; bloquea la fila."""
        ...

    def add_registration_view(
        self, registration: EventRegistration, projected_at: datetime
    ) -> None: ...

    def add_processed(
        self, message_id: UUID, event_type: str, aggregate_id: UUID, processed_at: datetime
    ) -> None: ...

    def commit(self) -> None:
        """Traduce un mensaje ya procesado o una inscripción ya proyectada por otra entrega a
        `DuplicateDelivery`, y una vista de evento creada en paralelo a `ConcurrentEventView`."""
        ...
