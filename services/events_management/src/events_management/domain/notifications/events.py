"""`notificacion.solicitada.v1` (ADR-045 §10, CMP-004).

Tópico `sportapp.eventos.v1`; agregado `notification`, `aggregate_id = notification_id`,
versión 1 y clave de orden `recipient_id` (las notificaciones de un mismo deportista llegan
en orden; las de un evento masivo se reparten entre muchas claves). El payload no lleva
correo, perfil ni datos de salud.
"""

from typing import Any

from events_management.domain.notifications.model import Notification
from events_management.domain.sport_events.created import utc_instant

EVENT_TYPE = "notificacion.solicitada.v1"
SCHEMA_VERSION = 1
AGGREGATE_TYPE = "notification"
AGGREGATE_VERSION = 1


def requested_payload(notification: Notification) -> dict[str, Any]:
    return {
        "notification_id": str(notification.notification_id),
        "recipient_id": str(notification.recipient_id),
        "type": notification.type.value,
        "priority": notification.priority.value,
        "event_id": str(notification.event_id),
        "title": notification.title,
        "body": notification.body,
        "created_at": utc_instant(notification.created_at),
    }
