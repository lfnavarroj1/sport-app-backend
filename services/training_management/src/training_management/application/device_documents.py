"""Representación del dispositivo de HU037 en respuestas y en el resultado idempotente guardado.

Los instantes se expresan en UTC con sufijo `Z`; `revoked_at` es `null` mientras está activo.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from training_management.domain.devices.model import (
    AthleteDevice,
    DeviceProtocol,
    DeviceStatus,
    DeviceType,
)
from training_management.domain.sessions.events import utc_instant


def device_document(device: AthleteDevice) -> dict[str, Any]:
    return {
        "device_id": device.device_id,
        "athlete_id": str(device.athlete_id),
        "display_name": device.display_name,
        "device_type": device.device_type.value,
        "protocol": device.protocol.value,
        "status": device.status.value,
        "version": device.version,
        "created_at": utc_instant(device.created_at),
        "updated_at": utc_instant(device.updated_at),
        "revoked_at": utc_instant(device.revoked_at) if device.revoked_at else None,
    }


def device_from_document(document: dict[str, Any]) -> AthleteDevice:
    revoked_at = document.get("revoked_at")
    return AthleteDevice(
        device_id=document["device_id"],
        athlete_id=UUID(document["athlete_id"]),
        display_name=document["display_name"],
        device_type=DeviceType(document["device_type"]),
        protocol=DeviceProtocol(document["protocol"]),
        status=DeviceStatus(document["status"]),
        version=int(document["version"]),
        created_at=datetime.fromisoformat(document["created_at"]),
        updated_at=datetime.fromisoformat(document["updated_at"]),
        revoked_at=datetime.fromisoformat(revoked_at) if revoked_at else None,
    )
