"""Agregado `AthleteDevice` de HU037 (ADR-044).

- Un único tipo: monitor de frecuencia cardiaca BLE con *Heart Rate Service*
  (`HEART_RATE_MONITOR` / `BLE_HRS`); ningún otro sensor.
- `device_id` es la identidad lógica que calcula el móvil: SHA-256 (64 hex en minúsculas) de la
  dirección BLE y el `user_id`. El backend nunca recibe ni guarda la dirección en claro.
- La propiedad es del deportista del token: la clave es (`athlete_id`, `device_id`).
- Estados: inexistente → `ACTIVE` (versión 1) → `REVOKED` (versión siguiente) → `ACTIVE` al
  volver a vincularlo (versión siguiente). Un dispositivo `REVOKED` no admite mediciones.
"""

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from training_management.domain.sessions.validation import Issue, Violation

DISPLAY_NAME_MIN = 1
DISPLAY_NAME_MAX = 60
FIRST_VERSION = 1
DEVICE_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class DeviceType(StrEnum):
    HEART_RATE_MONITOR = "HEART_RATE_MONITOR"


class DeviceProtocol(StrEnum):
    BLE_HRS = "BLE_HRS"


class DeviceStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class DeviceIssue(StrEnum):
    """Motivos propios de HU037 que se suman al catálogo de ADR-029."""

    INVALID_FORMAT = "invalid_format"
    INVALID_LENGTH = "invalid_length"


class InvalidDevice(Exception):
    """La solicitud incumple ADR-044; informa todas las reglas incumplidas."""

    code = "validation_error"

    def __init__(self, violations: list[tuple[str | None, str]]) -> None:
        super().__init__(", ".join(f"{field}: {issue}" for field, issue in violations))
        self.violations = tuple(violations)


class DeviceNotFound(Exception):
    """No existe o pertenece a otro deportista (no se revela cuál)."""

    code = "device_not_found"


class DeviceAlreadyRegistered(Exception):
    """El mismo `device_id` ya está vinculado y activo con otro nombre."""

    code = "device_already_registered"


class DeviceRevoked(Exception):
    """CA6: el dispositivo fue revocado; no se aceptan sus mediciones."""

    code = "device_revoked"


@dataclass(frozen=True)
class DeviceInput:
    """Vinculación tal como llega; `None` si el campo faltaba o tenía otro tipo JSON."""

    device_id: str | None
    display_name: str | None
    device_type: str | None
    protocol: str | None
    transport_violations: tuple[Violation, ...] = ()


@dataclass(frozen=True)
class DeviceData:
    device_id: str
    display_name: str
    device_type: DeviceType
    protocol: DeviceProtocol


@dataclass(frozen=True)
class AthleteDevice:
    device_id: str
    athlete_id: UUID
    display_name: str
    device_type: DeviceType
    protocol: DeviceProtocol
    status: DeviceStatus
    version: int
    created_at: datetime
    updated_at: datetime
    revoked_at: datetime | None

    @classmethod
    def register(cls, *, athlete_id: UUID, data: DeviceData, now: datetime) -> "AthleteDevice":
        instant = now.astimezone(UTC)
        return cls(
            device_id=data.device_id,
            athlete_id=athlete_id,
            display_name=data.display_name,
            device_type=data.device_type,
            protocol=data.protocol,
            status=DeviceStatus.ACTIVE,
            version=FIRST_VERSION,
            created_at=instant,
            updated_at=instant,
            revoked_at=None,
        )

    @property
    def is_active(self) -> bool:
        return self.status is DeviceStatus.ACTIVE

    def same_registration(self, data: DeviceData) -> bool:
        """La misma vinculación (nombre, tipo y protocolo) que ya está activa."""
        return (
            self.display_name == data.display_name
            and self.device_type is data.device_type
            and self.protocol is data.protocol
        )

    def revoke(self, now: datetime) -> "AthleteDevice":
        if not self.is_active:
            raise ValueError("Solo se revoca un dispositivo activo.")
        instant = now.astimezone(UTC)
        return replace(
            self,
            status=DeviceStatus.REVOKED,
            version=self.version + 1,
            updated_at=instant,
            revoked_at=instant,
        )

    def reactivate(self, data: DeviceData, now: datetime) -> "AthleteDevice":
        """Vincular de nuevo un dispositivo revocado (mismo `device_id`, nombre vigente)."""
        if self.is_active:
            raise ValueError("Solo se reactiva un dispositivo revocado.")
        return replace(
            self,
            display_name=data.display_name,
            device_type=data.device_type,
            protocol=data.protocol,
            status=DeviceStatus.ACTIVE,
            version=self.version + 1,
            updated_at=now.astimezone(UTC),
            revoked_at=None,
        )


def device_id_problem(value: str) -> str | None:
    """`invalid_format` si no son 64 hexadecimales en minúsculas (SHA-256)."""
    return None if DEVICE_ID_PATTERN.match(value) else DeviceIssue.INVALID_FORMAT.value


def validate_device_id(value: str) -> str:
    problem = device_id_problem(value)
    if problem is not None:
        raise InvalidDevice([("device_id", problem)])
    return value


def _choice[E: StrEnum](
    enum: type[E], value: str | None, field: str, violations: list[tuple[str | None, str]]
) -> E | None:
    if value is None:
        return None
    if value in {item.value for item in enum}:
        return enum(value)
    violations.append((field, Issue.UNKNOWN_VALUE.value))
    return None


def validate_device(raw: DeviceInput) -> DeviceData:
    """Vinculación normalizada o `InvalidDevice` con todas las reglas incumplidas."""
    violations: list[tuple[str | None, str]] = [
        (item.field, item.issue.value) for item in raw.transport_violations
    ]
    device_id = None
    if raw.device_id is not None:
        problem = device_id_problem(raw.device_id)
        if problem is None:
            device_id = raw.device_id
        else:
            violations.append(("device_id", problem))
    display_name = None
    if raw.display_name is not None:
        trimmed = raw.display_name.strip()
        if DISPLAY_NAME_MIN <= len(trimmed) <= DISPLAY_NAME_MAX:
            display_name = trimmed
        else:
            violations.append(("display_name", DeviceIssue.INVALID_LENGTH.value))
    device_type = _choice(DeviceType, raw.device_type, "device_type", violations)
    protocol = _choice(DeviceProtocol, raw.protocol, "protocol", violations)
    if (
        violations
        or device_id is None
        or display_name is None
        or device_type is None
        or protocol is None
    ):
        raise InvalidDevice(violations)
    return DeviceData(
        device_id=device_id,
        display_name=display_name,
        device_type=device_type,
        protocol=protocol,
    )
