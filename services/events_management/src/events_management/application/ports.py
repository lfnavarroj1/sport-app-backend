"""Puertos que la aplicación necesita; la infraestructura los implementa."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID

from events_management.domain.notifications.model import Notification, NotificationPreferences
from events_management.domain.registrations.model import (
    EventForRegistration,
    EventRegistration,
)
from events_management.domain.sport_events.fee import EventFee
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
    # HU025 (ADR-040): tarifa informativa proyectada; `None` sin tarifa.
    fee: EventFee | None = None


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


class RegistrationCounter(Protocol):
    """HU025 (ADR-040): inscripciones de cada evento contadas en la base de escritura (el
    dominio posee ambas tablas); no hay contador en `event_read` (CMP-004)."""

    def counts(self, event_ids: Collection[UUID]) -> dict[UUID, int]:
        """Conteo por evento; un evento sin inscripciones no aparece (equivale a 0)."""
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
    # HU039: clave de orden distinta del agregado (`recipient_id`); `None` = `aggregate_id`.
    ordering_key: str | None = None


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


class FeeUnitOfWork(Transaction, Protocol):
    """Transacción local de HU025: evento bloqueado, tarifa, versión, idempotencia y outbox."""

    def find_idempotency(
        self, actor_id: UUID, operation: str, key: UUID
    ) -> IdempotencyRecord | None: ...

    def delete_idempotency(self, actor_id: UUID, operation: str, key: UUID) -> None: ...

    def lock_event(self, event_id: UUID) -> SportEvent | None:
        """Bloquea la fila del evento: dos cambios concurrentes se aplican en serie."""
        ...

    def update_fee(self, event: SportEvent) -> None:
        """Guarda tarifa, `fee_updated_at`, versión y `updated_at` del evento bloqueado."""
        ...

    def count_registrations(self, event_id: UUID) -> int: ...

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
    # HU025: el organizador que consulta es el propietario; solo entonces se compone
    # `registration_count` (base de escritura).
    owned_by_caller: bool = False
    registration_count: int | None = None


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

    def inbox_contains(self, notification_id: UUID) -> bool:
        """HU039: la notificación ya está en la bandeja (otra entrega la proyectó)."""
        ...

    def add_inbox_item(self, notification: Notification, projected_at: datetime) -> None: ...

    def add_processed(
        self, message_id: UUID, event_type: str, aggregate_id: UUID, processed_at: datetime
    ) -> None: ...

    def commit(self) -> None:
        """Traduce un mensaje ya procesado o una inscripción ya proyectada por otra entrega a
        `DuplicateDelivery`, y una vista de evento creada en paralelo a `ConcurrentEventView`."""
        ...


@dataclass(frozen=True)
class AttendeePosition:
    """HU022: posición del cursor en el orden `registered_at`, `registration_id`."""

    registered_at: datetime
    registration_id: UUID


@dataclass(frozen=True)
class AttendeesQuery:
    event_id: UUID
    organizer_id: UUID
    status: str | None
    after: AttendeePosition | None
    limit: int


@dataclass(frozen=True)
class AttendeePage:
    """Inscripciones de la página y conteo de todas las del evento por estado."""

    items: tuple[EventRegistration, ...]
    by_status: dict[str, int]


class AttendeeReader(Protocol):
    def attendees(self, query: AttendeesQuery) -> AttendeePage | None:
        """`None` si el evento no existe o no es del organizador; página y conteos de una
        misma lectura en otro caso."""
        ...


# HU039 (ADR-045): notificaciones de eventos masivos.


class StoreUnavailable(Exception):
    """La base del dominio no respondió; la API responde `503 upstream_unavailable`."""

    code = "upstream_unavailable"


class RecipientsUnavailable(Exception):
    """Usuarios no respondió la búsqueda de destinatarios: el mensaje se reintenta."""


@dataclass(frozen=True)
class AthletePage:
    user_ids: tuple[UUID, ...]
    next_cursor: str | None


class AthleteDirectory(Protocol):
    """Contrato interno `POST /v1/users/athletes/search` de Usuarios."""

    def search(
        self, *, sports: Sequence[str], city: str, country_code: str, cursor: str | None, limit: int
    ) -> AthletePage:
        """Una página de deportistas coincidentes; `RecipientsUnavailable` si falla."""
        ...


@dataclass(frozen=True)
class DueReminder:
    """Inscripción cuyo evento empieza dentro de la ventana del recordatorio."""

    registration_id: UUID
    athlete_id: UUID
    event: SportEvent


class NotificationUnitOfWork(Transaction, Protocol):
    """Transacción de escritura de un lote de notificaciones (≤ 100) con su outbox."""

    def preferences(self, recipient_ids: Collection[UUID]) -> dict[UUID, NotificationPreferences]:
        """Preferencias registradas; quien no aparece usa los valores por defecto."""
        ...

    def add_notifications(self, notifications: Sequence[Notification]) -> list[Notification]:
        """Inserta las que no existen por `dedupe_key` y devuelve solo las insertadas."""
        ...

    def due_reminders(
        self, starts_from: datetime, starts_to: datetime, after: UUID | None, limit: int
    ) -> list[DueReminder]:
        """Inscripciones con `starts_at` en `[starts_from, starts_to]` en orden de
        `registration_id`, estrictamente después de `after`; `FOR UPDATE SKIP LOCKED`."""
        ...

    def add_outbox(self, message: OutboxMessage) -> None: ...

    def commit(self) -> None: ...


class PreferencesConflict(Exception):
    """Otra solicitud concurrente creó primero las preferencias del deportista."""


class PreferencesUnitOfWork(Transaction, Protocol):
    def lock_preferences(self, recipient_id: UUID) -> NotificationPreferences | None:
        """Preferencias registradas, con la fila bloqueada; `None` si no hay."""
        ...

    def save_preferences(
        self, recipient_id: UUID, preferences: NotificationPreferences, updated_at: datetime
    ) -> None: ...

    def commit(self) -> None:
        """`PreferencesConflict` si otra solicitud insertó primero."""
        ...


class PreferencesReader(Protocol):
    def preferences(self, recipient_id: UUID) -> NotificationPreferences: ...


class PushTokenStore(Protocol):
    def register(self, recipient_id: UUID, token: str, platform: str, now: datetime) -> None:
        """Guarda el token cifrado; si pertenecía a otro deportista, se reasigna."""
        ...


@dataclass(frozen=True)
class InboxItem:
    notification_id: UUID
    type: str
    priority: str
    event_id: UUID
    title: str
    body: str
    created_at: datetime
    read_at: datetime | None


@dataclass(frozen=True)
class InboxPosition:
    """Posición del cursor en el orden `created_at DESC, notification_id DESC`."""

    created_at: datetime
    notification_id: UUID


class InboxReader(Protocol):
    def page(self, recipient_id: UUID, after: InboxPosition | None, limit: int) -> list[InboxItem]:
        """Hasta `limit` notificaciones propias, estrictamente después de `after`."""
        ...

    def mark_read(
        self, recipient_id: UUID, notification_id: UUID, now: datetime
    ) -> InboxItem | None:
        """Fija `read_at` si estaba vacío; `None` si la notificación no es suya o no existe."""
        ...


@dataclass(frozen=True)
class PushTarget:
    token_id: UUID
    token: str


class PushDeliveryStore(Protocol):
    """Base de escritura vista por el envío *push* del proyector (solo con FCM)."""

    def push_enabled(self, recipient_id: UUID) -> bool: ...

    def targets(self, recipient_id: UUID) -> list[PushTarget]: ...

    def sent_token_ids(self, notification_id: UUID) -> set[UUID]:
        """Tokens a los que la notificación ya se entregó (`SENT`)."""
        ...

    def record(
        self,
        notification_id: UUID,
        token_id: UUID,
        *,
        sent: bool,
        attempts: int,
        error: str | None,
        now: datetime,
    ) -> None: ...


class PushRejected(Exception):
    """FCM rechazó el mensaje de forma definitiva (no se reintenta)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PushFailed(Exception):
    """Fallo transitorio de FCM; `retry_after` en segundos si FCM lo indicó."""

    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


class PushSender(Protocol):
    def send(self, token: str, data: dict[str, str]) -> None:
        """Envía un mensaje de datos; `PushRejected` o `PushFailed` si no se entrega."""
        ...
