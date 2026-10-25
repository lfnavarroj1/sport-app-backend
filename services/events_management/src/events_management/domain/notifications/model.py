"""Notificaciones de eventos masivos para la bandeja del deportista (HU039, ADR-045).

- Tipos del MVP: `EVENT_PUBLISHED_MATCH` (evento publicado que coincide con los deportes y la
  ciudad del deportista, prioridad `NORMAL`) y `EVENT_REMINDER_24H` (recordatorio a los
  inscritos 24 h antes de `starts_at`, prioridad `HIGH`).
- Una sola notificación por `dedupe_key = <type>:<event_id>:<recipient_id>` (CA3).
- Las preferencias vigentes deciden si se genera cada tipo (CA2); sin preferencias
  registradas rigen los valores por defecto (todo habilitado, versión 0).
- El texto no lleva correo, perfil ni fechas con zona: la app convierte `starts_at` del
  detalle del evento (HU041) a la zona del dispositivo.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from events_management.domain.sport_events.model import SportEvent

TITLE_MAX = 120
BODY_MAX = 500
# Planificador (ADR-045 §8): cada 15 min genera recordatorios para los eventos cuyo inicio
# cae en la ventana cerrada [ahora + 23 h 45 min, ahora + 24 h 15 min].
REMINDER_WINDOW_START = timedelta(hours=23, minutes=45)
REMINDER_WINDOW_END = timedelta(hours=24, minutes=15)


class NotificationType(StrEnum):
    EVENT_PUBLISHED_MATCH = "EVENT_PUBLISHED_MATCH"
    EVENT_REMINDER_24H = "EVENT_REMINDER_24H"


class NotificationPriority(StrEnum):
    NORMAL = "NORMAL"
    HIGH = "HIGH"


PRIORITIES = {
    NotificationType.EVENT_PUBLISHED_MATCH: NotificationPriority.NORMAL,
    NotificationType.EVENT_REMINDER_24H: NotificationPriority.HIGH,
}


@dataclass(frozen=True)
class Notification:
    notification_id: UUID
    recipient_id: UUID
    type: NotificationType
    priority: NotificationPriority
    event_id: UUID
    title: str
    body: str
    created_at: datetime

    @property
    def dedupe_key(self) -> str:
        return dedupe_key(self.type, self.event_id, self.recipient_id)


@dataclass(frozen=True)
class NotificationPreferences:
    event_matches: bool = True
    event_reminders: bool = True
    push_enabled: bool = True
    version: int = 0

    def allows(self, notification_type: NotificationType) -> bool:
        if notification_type is NotificationType.EVENT_PUBLISHED_MATCH:
            return self.event_matches
        return self.event_reminders


DEFAULT_PREFERENCES = NotificationPreferences()


class VersionConflict(Exception):
    """`expected_version` no es la versión vigente de las preferencias."""

    code = "version_conflict"

    def __init__(self, current_version: int) -> None:
        super().__init__(str(current_version))
        self.current_version = current_version


def dedupe_key(notification_type: NotificationType, event_id: UUID, recipient_id: UUID) -> str:
    return f"{notification_type.value}:{event_id}:{recipient_id}"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _texts(notification_type: NotificationType, event: SportEvent) -> tuple[str, str]:
    details = event.details
    if notification_type is NotificationType.EVENT_PUBLISHED_MATCH:
        title = f"Nuevo evento: {details.name}"
        body = f"{details.discipline} en {details.location.city}. Toca para ver el detalle."
    else:
        title = f"Tu evento es mañana: {details.name}"
        body = (
            f"{details.location.name}, {details.location.city}. "
            "Revisa la hora de inicio en el detalle."
        )
    return _clip(title, TITLE_MAX), _clip(body, BODY_MAX)


def new_notification(
    *,
    notification_id: UUID,
    notification_type: NotificationType,
    event: SportEvent,
    recipient_id: UUID,
    now: datetime,
) -> Notification:
    title, body = _texts(notification_type, event)
    return Notification(
        notification_id=notification_id,
        recipient_id=recipient_id,
        type=notification_type,
        priority=PRIORITIES[notification_type],
        event_id=event.event_id,
        title=title,
        body=body,
        created_at=now.astimezone(UTC),
    )


def reminder_window(now: datetime) -> tuple[datetime, datetime]:
    """Inicio y fin (ambos incluidos) de `starts_at` para los recordatorios de esta corrida."""
    instant = now.astimezone(UTC)
    return instant + REMINDER_WINDOW_START, instant + REMINDER_WINDOW_END


def changed_preferences(
    current: NotificationPreferences,
    *,
    event_matches: bool,
    event_reminders: bool,
    push_enabled: bool,
    expected_version: int,
) -> NotificationPreferences:
    """Preferencias nuevas con la versión siguiente; un conflicto nunca se sobrescribe."""
    if expected_version != current.version:
        raise VersionConflict(current.version)
    return NotificationPreferences(
        event_matches=event_matches,
        event_reminders=event_reminders,
        push_enabled=push_enabled,
        version=current.version + 1,
    )
