"""`servicio.publicado.v1` (HU019, ADR-027).

Tópico `sportapp.servicios.v1`; agregado y clave de orden: `service_id`. El payload es
mínimo (`service_id`, `provider_id`, `status` y `published_at` en UTC `Z`): no transporta
nombre, descripción, tarifa ni disponibilidad; un consumidor autorizado consulta el estado
completo en la API propietaria.
"""

from datetime import UTC, datetime
from typing import Any

from complementary_services.domain.catalog.offer import ServiceOffer, ServiceStatus

EVENT_TYPE = "servicio.publicado.v1"
SCHEMA_VERSION = 1
AGGREGATE_TYPE = "service"


def utc_instant(value: datetime) -> str:
    """ISO 8601 en UTC con sufijo `Z`; la conversión a la zona del usuario es del frontend."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def published_payload(offer: ServiceOffer) -> dict[str, Any]:
    """Payload de `servicio.publicado.v1` para un servicio ya publicado."""
    if offer.status is not ServiceStatus.PUBLISHED or offer.published_at is None:
        raise ValueError("El servicio no está publicado.")
    return {
        "service_id": str(offer.service_id),
        "provider_id": str(offer.provider_id),
        "status": ServiceStatus.PUBLISHED.value,
        "published_at": utc_instant(offer.published_at),
    }
