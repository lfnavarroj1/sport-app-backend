"""`evento.publicado.v1` (ADR-033, CMP-004).

Tópico `sportapp.eventos.v1`; agregado `event`, clave de orden `event_id` y versión la
resultante de la publicación. El payload lleva el estado completo del evento más
`published_at`, en UTC (`Z`); sin inscripciones ni credenciales. Desde HU025 (ADR-040) el
estado completo incluye `fee` y `fee_updated_at` (por ratificar): sin ellos, publicar un
borrador con tarifa la borraría de `event_read` al aplicar la versión mayor.
"""

from typing import Any

from events_management.domain.sport_events.created import event_document, utc_instant
from events_management.domain.sport_events.fee import fee_document
from events_management.domain.sport_events.model import SportEvent

EVENT_TYPE = "evento.publicado.v1"
SCHEMA_VERSION = 1
AGGREGATE_TYPE = "event"


def aggregate_document(event: SportEvent) -> dict[str, Any]:
    """Agregado completo de ADR-028 con `published_at` (`null` en borrador) y la tarifa de
    HU025 (`fee` y `fee_updated_at`, `null` sin tarifa).

    Es la respuesta de publicar, la base de cada elemento de `GET /v1/me/events` y el payload
    de `evento.publicado.v1` y de `evento.tarifa_actualizada.v1`.
    """
    return {
        **event_document(event),
        "published_at": utc_instant(event.published_at) if event.published_at else None,
        "fee": fee_document(event.fee),
        "fee_updated_at": utc_instant(event.fee_updated_at) if event.fee_updated_at else None,
    }


def owned_event_document(event: SportEvent, registration_count: int) -> dict[str, Any]:
    """HU025: el agregado como lo ve su organizador, con las inscripciones vigentes para
    advertir el cambio de tarifa (CA4). Respuesta de `PUT`/`DELETE` de la tarifa y cada
    elemento de `GET /v1/me/events`."""
    return {**aggregate_document(event), "registration_count": registration_count}
