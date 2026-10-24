"""Dobles en memoria para probar HU037 sin infraestructura."""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from training_management.application.errors import DeviceConflict, IdempotencyKeyConflict
from training_management.application.ports import CommandRecord
from training_management.domain.devices.model import AthleteDevice
from training_management.domain.measurements.model import Measurement
from training_management.domain.sessions.model import TrainingSession


def device_hash(address: str, user_id: UUID) -> str:
    """Lo que calcula el móvil: SHA-256 de la dirección BLE y el usuario (ADR-044 §2)."""
    return hashlib.sha256(f"{address}{user_id}".encode()).hexdigest()


def device_body(device_id: str, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "device_id": device_id,
        "display_name": "Banda pecho",
        "device_type": "HEART_RATE_MONITOR",
        "protocol": "BLE_HRS",
    }
    values.update(overrides)
    return values


def sample(measured_at: str, value: int = 120, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "measured_at": measured_at,
        "type": "HEART_RATE",
        "value": value,
        "unit": "bpm",
    }
    values.update(overrides)
    return values


@dataclass
class InMemoryDeviceStore:
    devices: dict[tuple[UUID, str], AthleteDevice] = field(default_factory=dict)
    commands: dict[tuple[UUID, str, UUID], CommandRecord] = field(default_factory=dict)
    sessions: dict[UUID, TrainingSession] = field(default_factory=dict)
    measurements: dict[tuple[UUID, str, datetime], Measurement] = field(default_factory=dict)


class InMemoryDeviceUnitOfWork:
    """Aplica los cambios solo al confirmar y valida las mismas claves que PostgreSQL."""

    def __init__(
        self, store: InMemoryDeviceStore, before_commit: Callable[[], None] | None = None
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self._added: list[AthleteDevice] = []
        self._updated: list[AthleteDevice] = []
        self._commands: list[CommandRecord] = []
        self._deleted: list[tuple[UUID, str, UUID]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._added, self._updated, self._commands, self._deleted = [], [], [], []

    def find_command(self, actor_id: UUID, operation: str, key: UUID) -> CommandRecord | None:
        return self.store.commands.get((actor_id, operation, key))

    def delete_command(self, actor_id: UUID, operation: str, key: UUID) -> None:
        self._deleted.append((actor_id, operation, key))

    def add_command(self, record: CommandRecord) -> None:
        self._commands.append(record)

    def get_device(
        self, athlete_id: UUID, device_id: str, *, for_update: bool = False
    ) -> AthleteDevice | None:
        return self.store.devices.get((athlete_id, device_id))

    def add_device(self, device: AthleteDevice) -> None:
        self._added.append(device)

    def update_device(self, device: AthleteDevice) -> None:
        self._updated.append(device)

    def commit(self) -> None:
        if self.before_commit is not None:
            hook, self.before_commit = self.before_commit, None
            hook()
        for key in self._deleted:
            self.store.commands.pop(key, None)
        if any((item.athlete_id, item.device_id) in self.store.devices for item in self._added):
            raise DeviceConflict()
        if any(
            (record.actor_id, record.operation, record.key) in self.store.commands
            for record in self._commands
        ):
            raise IdempotencyKeyConflict()
        for device in [*self._added, *self._updated]:
            self.store.devices[(device.athlete_id, device.device_id)] = device
        for record in self._commands:
            self.store.commands[(record.actor_id, record.operation, record.key)] = record


class InMemoryDeviceReader:
    def __init__(self, store: InMemoryDeviceStore) -> None:
        self.store = store

    def list(self, athlete_id: UUID) -> list[AthleteDevice]:
        own = [item for item in self.store.devices.values() if item.athlete_id == athlete_id]
        return sorted(own, key=lambda item: (item.created_at, item.device_id), reverse=True)


class InMemoryMeasurementUnitOfWork:
    def __init__(self, store: InMemoryDeviceStore) -> None:
        self.store = store
        self._pending: dict[tuple[UUID, str, datetime], Measurement] = {}
        self.committed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._pending = {}

    def get_session_for_ingestion(self, session_id: UUID) -> TrainingSession | None:
        return self.store.sessions.get(session_id)

    def get_device_for_ingestion(self, athlete_id: UUID, device_id: str) -> AthleteDevice | None:
        return self.store.devices.get((athlete_id, device_id))

    def add_measurements(
        self, session_id: UUID, device_id: str, measurements: tuple[Measurement, ...]
    ) -> int:
        inserted = 0
        for item in measurements:
            key = (session_id, device_id, item.measured_at)
            if key not in self.store.measurements and key not in self._pending:
                self._pending[key] = item
                inserted += 1
        return inserted

    def commit(self) -> None:
        self.store.measurements.update(self._pending)
        self.committed = True
