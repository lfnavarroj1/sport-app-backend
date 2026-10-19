"""`evento.publicado.v1` (ADR-033, CMP-004).

Tópico `sportapp.eventos.v1`; agregado `event`, clave de orden `event_id` y versión la
resultante de la publicación. El payload lleva el estado completo del evento más
`published_at`, en UTC (`Z`); sin tarifa, inscripciones ni credenciales.
"""

from typing import Any

from events_management.domain.sport_events.created import event_document, utc_instant
from events_management.domain.sport_events.model import SportEvent

EVENT_TYPE = "evento.publicado.v1"
SCHEMA_VERSION = 1
AGGREGATE_TYPE = "event"


def aggregate_document(event: SportEvent) -> dict[str, Any]:
    """Agregado completo de ADR-028 con `published_at` (`null` en borrador).

    Es la respuesta de publicar, cada elemento de `GET /v1/me/events` y, para un evento
    publicado, el payload de `evento.publicado.v1`.
    """
    return {
        **event_document(event),
        "published_at": utc_instant(event.published_at) if event.published_at else None,
    }
