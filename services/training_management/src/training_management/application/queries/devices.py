"""HU037: dispositivos propios del deportista (`GET /v1/me/devices`, ADR-044 §3).

Lee `athlete_device` de la base de escritura: la vinculación no emite eventos, así que no hay
proyección. Incluye los dispositivos `ACTIVE` y `REVOKED`.
"""

from training_management.application.access import require_devices_capability
from training_management.application.ports import Caller, CapabilityChecker, DeviceReader
from training_management.domain.devices.model import AthleteDevice


class ListMyDevices:
    def __init__(self, *, reader: DeviceReader, capabilities: CapabilityChecker) -> None:
        self._reader = reader
        self._capabilities = capabilities

    def __call__(self, caller: Caller) -> list[AthleteDevice]:
        require_devices_capability(caller, self._capabilities)
        return self._reader.list(caller.user_id)
