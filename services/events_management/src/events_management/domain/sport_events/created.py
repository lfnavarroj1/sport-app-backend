"""`evento.creado.v1` (ADR-028, CMP-004).

Tópico `sportapp.eventos.v1`; agregado y clave de orden: `event_id`. El payload lleva el
estado completo que necesita `eventos-read-projector` para construir `event_read`, con
los instantes en UTC (`Z`). No transporta credenciales ni datos de pago.
"""

from datetime import UTC, datetime
from typing import Any

from events_management.domain.sport_events.model import SportEvent

EVENT_TYPE = "evento.creado.v1"
SCHEMA_VERSION = 1
AGGREGATE_TYPE = "event"


def utc_instant(value: datetime) -> str:
    """ISO 8601 en UTC con sufijo `Z`; la conversión a la zona del usuario es del frontend."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def event_document(event: SportEvent) -> dict[str, Any]:
    """Representación pública del evento: respuesta del comando y payload del evento."""
    details = event.details
    return {
        "event_id": str(event.event_id),
        "organizer_id": str(event.organizer_id),
        "name": details.name,
        "description": details.description,
        "discipline": details.discipline,
        "location": {
            "name": details.location.name,
            "address": details.location.address,
            "city": details.location.city,
            "country_code": details.location.country_code,
        },
        "starts_at": utc_instant(details.starts_at),
        "ends_at": utc_instant(details.ends_at),
        "time_zone": details.time_zone,
        "duration_minutes": details.duration_minutes,
        "capacity": details.capacity,
        "status": event.status.value,
        "version": event.version,
        "created_at": utc_instant(event.created_at),
        "updated_at": utc_instant(event.updated_at),
    }
