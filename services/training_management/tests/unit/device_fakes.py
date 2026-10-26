"""Dobles en memoria para probar HU037 y HU036 sin infraestructura."""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from training_management.application.errors import (
    AthleteContextUnavailable,
    DeviceConflict,
    IdempotencyKeyConflict,
    MonitoringSettingsConflict,
)
from training_management.application.ports import CommandRecord, MonitoringState, OutboxMessage
from training_management.domain.devices.model import AthleteDevice
from training_management.domain.measurements.model import Measurement
from training_management.domain.monitoring.model import MonitoringSettings, RiskAlert
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
    states: dict[UUID, MonitoringState] = field(default_factory=dict)
    alerts: dict[UUID, tuple[RiskAlert, int]] = field(default_factory=dict)
    outbox: list[OutboxMessage] = field(default_factory=list)
    settings: dict[UUID, MonitoringSettings] = field(default_factory=dict)


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
    """Aplica muestras, alertas, estado y outbox solo al confirmar (como la transacción)."""

    def __init__(self, store: InMemoryDeviceStore) -> None:
        self.store = store
        self._pending: dict[tuple[UUID, str, datetime], Measurement] = {}
        self._alerts: dict[UUID, tuple[RiskAlert, int]] = {}
        self._outbox: list[OutboxMessage] = []
        self._state: MonitoringState | None = None
        self.committed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._pending, self._alerts, self._outbox, self._state = {}, {}, [], None

    def get_session_for_ingestion(self, session_id: UUID) -> TrainingSession | None:
        return self.store.sessions.get(session_id)

    def get_device_for_ingestion(self, athlete_id: UUID, device_id: str) -> AthleteDevice | None:
        return self.store.devices.get((athlete_id, device_id))

    def lock_monitoring_state(self, session_id: UUID, now: datetime) -> MonitoringState:
        return self.store.states.get(session_id, MonitoringState(session_id, 0, 0))

    def add_measurements(
        self, session_id: UUID, device_id: str, measurements: tuple[Measurement, ...]
    ) -> tuple[Measurement, ...]:
        inserted: list[Measurement] = []
        for item in measurements:
            key = (session_id, device_id, item.measured_at)
            if key not in self.store.measurements and key not in self._pending:
                self._pending[key] = item
                inserted.append(item)
        return tuple(inserted)

    def _samples(self, session_id: UUID, device_id: str) -> list[tuple[datetime, int]]:
        everything = {**self.store.measurements, **self._pending}
        return sorted(
            (instant, item.value)
            for (session, device, instant), item in everything.items()
            if session == session_id and device == device_id
        )

    def latest_measured_at(self, session_id: UUID, device_id: str) -> datetime | None:
        samples = self._samples(session_id, device_id)
        return samples[-1][0] if samples else None

    def window_samples(
        self, session_id: UUID, device_id: str, since: datetime
    ) -> list[tuple[datetime, int]]:
        return [item for item in self._samples(session_id, device_id) if item[0] >= since]

    def alert_instants(self, session_id: UUID, since: datetime, until: datetime) -> list[datetime]:
        alerts = {**self.store.alerts, **self._alerts}.values()
        return sorted(
            alert.detected_at
            for alert, _ in alerts
            if alert.session_id == session_id and since <= alert.detected_at <= until
        )

    def add_alerts(
        self, alerts: tuple[RiskAlert, ...], first_sequence: int, created_at: datetime
    ) -> None:
        for offset, alert in enumerate(alerts):
            if alert.alert_id in self.store.alerts or alert.alert_id in self._alerts:
                raise AssertionError("alert_id duplicado")
            self._alerts[alert.alert_id] = (alert, first_sequence + offset)

    def save_monitoring_state(self, state: MonitoringState, now: datetime) -> None:
        self._state = state

    def add_outbox(self, message: OutboxMessage) -> None:
        self._outbox.append(message)

    def commit(self) -> None:
        self.store.measurements.update(self._pending)
        self.store.alerts.update(self._alerts)
        self.store.outbox.extend(self._outbox)
        if self._state is not None:
            self.store.states[self._state.session_id] = self._state
        self.committed = True


class InMemoryMonitoringSettingsUnitOfWork:
    def __init__(
        self, store: InMemoryDeviceStore, before_commit: Callable[[], None] | None = None
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self._added: list[MonitoringSettings] = []
        self._updated: list[MonitoringSettings] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._added, self._updated = [], []

    def get_settings(
        self, athlete_id: UUID, *, for_update: bool = False
    ) -> MonitoringSettings | None:
        return self.store.settings.get(athlete_id)

    def add_settings(self, settings: MonitoringSettings) -> None:
        self._added.append(settings)

    def update_settings(self, settings: MonitoringSettings) -> None:
        self._updated.append(settings)

    def commit(self) -> None:
        if self.before_commit is not None:
            hook, self.before_commit = self.before_commit, None
            hook()
        if any(item.athlete_id in self.store.settings for item in self._added):
            raise MonitoringSettingsConflict()
        for item in [*self._added, *self._updated]:
            self.store.settings[item.athlete_id] = item


@dataclass
class FakeBirthDates:
    """Contexto de Usuarios: fecha por deportista; `down` simula la caída."""

    dates: dict[UUID, date | None] = field(default_factory=dict)
    down: bool = False
    calls: int = 0

    def birth_date(self, athlete_id: UUID) -> date | None:
        self.calls += 1
        if self.down:
            raise AthleteContextUnavailable()
        return self.dates.get(athlete_id)


def custom_settings(athlete_id: UUID, threshold: int, now: datetime) -> MonitoringSettings:
    return MonitoringSettings.create(athlete_id, threshold, now)
