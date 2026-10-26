"""HU037: vincular y revocar monitores de frecuencia cardiaca (ADR-044 §1-§3).

Vinculación (`POST /v1/me/devices`, CA1):
1. Solo `athlete`; validar el cuerpo (tipo `HEART_RATE_MONITOR`, protocolo `BLE_HRS`,
   `device_id` SHA-256 y nombre de 1 a 60 caracteres).
2. Verificar `devices.measurement.connect` en Afiliación **fuera** de la transacción.
3. Clave de idempotencia por deportista y operación, vigente 24 horas: la misma solicitud
   devuelve el resultado original; con el mismo dispositivo y otro nombre,
   `device_already_registered`; con otro dispositivo, `idempotency_key_reused`.
4. En una transacción con la fila bloqueada:
   - sin vínculo previo → `ACTIVE` versión 1 (`201`);
   - `ACTIVE` con la misma vinculación → se devuelve tal cual (`200`; la clave lo recuerda);
   - `ACTIVE` con otro nombre → `device_already_registered`;
   - `REVOKED` → vuelve a `ACTIVE` con la versión siguiente y el nombre recibido (`201`).
   Sin evento: la vinculación no interesa a otros dominios (ADR-044 §3).

Revocación (`DELETE /v1/me/devices/{device_id}`, CA6), en una transacción con la fila
bloqueada: inexistente o ajeno → `device_not_found`; ya revocado → se devuelve tal cual
(revocación idempotente); si no, `REVOKED` con la versión siguiente. Desde ese COMMIT la
ingestión responde `409 device_revoked`.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from training_management.application.access import require_athlete, require_devices_capability
from training_management.application.device_documents import (
    device_document,
    device_from_document,
)
from training_management.application.errors import (
    CapabilitiesUnavailable,
    CapabilityRequired,
    DeviceConflict,
    IdempotencyKeyConflict,
    IdempotencyKeyReused,
    PermissionRequired,
)
from training_management.application.ports import (
    Caller,
    CapabilityChecker,
    Clock,
    CommandRecord,
    DeviceUnitOfWork,
)
from training_management.domain.devices.model import (
    AthleteDevice,
    DeviceAlreadyRegistered,
    DeviceData,
    DeviceInput,
    DeviceNotFound,
    InvalidDevice,
    validate_device,
    validate_device_id,
)

REGISTER_OPERATION = "register_device"
IDEMPOTENCY_TTL = timedelta(hours=24)

_audit = logging.getLogger("sportapp.audit")

_REJECTIONS = (
    PermissionRequired,
    CapabilityRequired,
    CapabilitiesUnavailable,
    InvalidDevice,
    IdempotencyKeyReused,
    DeviceAlreadyRegistered,
    DeviceNotFound,
)


@dataclass(frozen=True)
class RegisterDeviceCommand:
    device: DeviceInput
    idempotency_key: UUID


@dataclass(frozen=True)
class DeviceResult:
    """`changed` es `False` si el vínculo ya estaba en el estado pedido (activo con la misma
    vinculación al vincular, o ya revocado al revocar)."""

    device: AthleteDevice
    changed: bool = True
    replayed: bool = False


def _fingerprint(athlete_id: UUID, data: DeviceData) -> str:
    canonical = json.dumps(
        [
            REGISTER_OPERATION,
            str(athlete_id),
            data.device_id,
            data.display_name,
            data.device_type.value,
            data.protocol.value,
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def device_resource_id(athlete_id: UUID, device_id: str) -> UUID:
    """Identificador estable del vínculo para `command_idempotency.resource_id` (UUID)."""
    return uuid5(NAMESPACE_URL, f"sportapp:athlete_device:{athlete_id}:{device_id}")


def _audit_result(name: str, caller: Caller, result: str, **fields: str) -> None:
    # Solo el deportista y el resultado: ni nombre, ni identidad del dispositivo, ni datos BLE.
    _audit.info(
        name, extra={"fields": {"athlete_id": str(caller.user_id), "result": result, **fields}}
    )


class RegisterDevice:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], DeviceUnitOfWork],
        capabilities: CapabilityChecker,
        clock: Clock,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._capabilities = capabilities
        self._clock = clock

    def validate(self, caller: Caller, raw: DeviceInput) -> DeviceData:
        """Actor y reglas de la vinculación, sin persistir ni consultar Afiliación."""
        require_athlete(caller)
        return validate_device(raw)

    def __call__(self, caller: Caller, command: RegisterDeviceCommand) -> DeviceResult:
        try:
            result = self._execute(caller, command)
        except _REJECTIONS as error:
            reason = getattr(error, "code", type(error).__name__)
            _audit_result("audit.device_registered", caller, "rejected", reason=reason)
            raise
        if result.replayed:
            outcome = "replayed"
        elif result.changed:
            outcome = "registered"
        else:
            outcome = "unchanged"
        _audit_result("audit.device_registered", caller, outcome)
        return result

    def _execute(self, caller: Caller, command: RegisterDeviceCommand) -> DeviceResult:
        data = self.validate(caller, command.device)
        require_devices_capability(caller, self._capabilities)
        fingerprint = _fingerprint(caller.user_id, data)
        previous = self._previous(caller.user_id, command.idempotency_key, fingerprint, data)
        if previous is not None:
            return previous
        try:
            return self._register(caller.user_id, data, command.idempotency_key, fingerprint)
        except (IdempotencyKeyConflict, DeviceConflict):
            # Otra solicitud confirmó primero: con la misma clave se devuelve su resultado; con
            # otra clave, esta se evalúa contra el vínculo que dejó la ganadora.
            previous = self._previous(caller.user_id, command.idempotency_key, fingerprint, data)
            if previous is not None:
                return previous
            return self._register(caller.user_id, data, command.idempotency_key, fingerprint)

    def _previous(
        self, athlete_id: UUID, key: UUID, fingerprint: str, data: DeviceData
    ) -> DeviceResult | None:
        with self._unit_of_work() as unit_of_work:
            record = unit_of_work.find_command(athlete_id, REGISTER_OPERATION, key)
        if record is None or record.expires_at <= self._clock.now():
            return None
        device = device_from_document(record.response["device"])
        if record.request_fingerprint != fingerprint:
            if device.device_id == data.device_id:
                raise DeviceAlreadyRegistered()
            raise IdempotencyKeyReused()
        return DeviceResult(device, changed=bool(record.response["changed"]), replayed=True)

    def _register(
        self, athlete_id: UUID, data: DeviceData, key: UUID, fingerprint: str
    ) -> DeviceResult:
        now = self._clock.now()
        with self._unit_of_work() as unit_of_work:
            existing = unit_of_work.find_command(athlete_id, REGISTER_OPERATION, key)
            if existing is not None:
                if existing.expires_at > now:
                    raise IdempotencyKeyConflict()
                unit_of_work.delete_command(athlete_id, REGISTER_OPERATION, key)
            current = unit_of_work.get_device(athlete_id, data.device_id, for_update=True)
            changed = True
            if current is None:
                device = AthleteDevice.register(athlete_id=athlete_id, data=data, now=now)
                unit_of_work.add_device(device)
            elif current.is_active:
                if not current.same_registration(data):
                    raise DeviceAlreadyRegistered()
                # Ya vinculado igual: no cambia nada, pero la clave recuerda este resultado.
                device, changed = current, False
            else:
                device = current.reactivate(data, now)
                unit_of_work.update_device(device)
            unit_of_work.add_command(
                CommandRecord(
                    actor_id=athlete_id,
                    operation=REGISTER_OPERATION,
                    key=key,
                    request_fingerprint=fingerprint,
                    resource_id=device_resource_id(athlete_id, device.device_id),
                    response={"device": device_document(device), "changed": changed},
                    created_at=now,
                    expires_at=now + IDEMPOTENCY_TTL,
                )
            )
            unit_of_work.commit()
        return DeviceResult(device, changed=changed)


class RevokeDevice:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], DeviceUnitOfWork],
        capabilities: CapabilityChecker,
        clock: Clock,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._capabilities = capabilities
        self._clock = clock

    def __call__(self, caller: Caller, device_id: str) -> DeviceResult:
        try:
            result = self._execute(caller, device_id)
        except _REJECTIONS as error:
            reason = getattr(error, "code", type(error).__name__)
            _audit_result("audit.device_revoked", caller, "rejected", reason=reason)
            raise
        _audit_result("audit.device_revoked", caller, "revoked" if result.changed else "unchanged")
        return result

    def _execute(self, caller: Caller, device_id: str) -> DeviceResult:
        require_athlete(caller)
        validate_device_id(device_id)
        require_devices_capability(caller, self._capabilities)
        with self._unit_of_work() as unit_of_work:
            device = unit_of_work.get_device(caller.user_id, device_id, for_update=True)
            if device is None:
                raise DeviceNotFound()
            if not device.is_active:
                return DeviceResult(device, changed=False)
            revoked = device.revoke(self._clock.now())
            unit_of_work.update_device(revoked)
            unit_of_work.commit()
        return DeviceResult(revoked)
