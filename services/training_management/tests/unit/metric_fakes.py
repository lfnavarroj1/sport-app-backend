"""Dobles en memoria y datos sintéticos para probar HU034 sin infraestructura."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

from training_management.application.errors import CalculationIdConflict, IdempotencyKeyConflict
from training_management.application.ports import CommandRecord, MetricResultQuery, OutboxMessage
from training_management.domain.metrics.catalog import (
    COOPER_12_MIN_V1,
    FTP_20_MIN_95_V1,
    InputDefinition,
    MethodDefinition,
    MetricCatalog,
)
from training_management.domain.metrics.model import TrainingMetricCalculation

METRICS_CAPABILITY = "training.metrics.ftp_vo2max"


@dataclass
class InMemoryMetricStore:
    calculations: dict[UUID, TrainingMetricCalculation] = field(default_factory=dict)
    outbox: list[OutboxMessage] = field(default_factory=list)
    commands: dict[tuple[UUID, str, UUID], CommandRecord] = field(default_factory=dict)


class InMemoryMetricUnitOfWork:
    """Aplica los cambios solo al confirmar y valida las mismas claves primarias que PostgreSQL."""

    def __init__(
        self, store: InMemoryMetricStore, before_commit: Callable[[], None] | None = None
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self._calculations: list[TrainingMetricCalculation] = []
        self._outbox: list[OutboxMessage] = []
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
        self._calculations, self._outbox, self._commands, self._deleted = [], [], [], []

    def find_command(self, actor_id: UUID, operation: str, key: UUID) -> CommandRecord | None:
        return self.store.commands.get((actor_id, operation, key))

    def delete_command(self, actor_id: UUID, operation: str, key: UUID) -> None:
        self._deleted.append((actor_id, operation, key))

    def add_command(self, record: CommandRecord) -> None:
        self._commands.append(record)

    def get_calculation(self, calculation_id: UUID) -> TrainingMetricCalculation | None:
        return self.store.calculations.get(calculation_id)

    def add_calculation(self, calculation: TrainingMetricCalculation) -> None:
        self._calculations.append(calculation)

    def add_outbox(self, message: OutboxMessage) -> None:
        self._outbox.append(message)

    def commit(self) -> None:
        if self.before_commit is not None:
            hook, self.before_commit = self.before_commit, None
            hook()
        for key in self._deleted:
            self.store.commands.pop(key, None)
        if any(
            (record.actor_id, record.operation, record.key) in self.store.commands
            for record in self._commands
        ):
            raise IdempotencyKeyConflict()
        if any(item.calculation_id in self.store.calculations for item in self._calculations):
            raise CalculationIdConflict()
        for calculation in self._calculations:
            self.store.calculations[calculation.calculation_id] = calculation
        self.store.outbox.extend(self._outbox)
        for record in self._commands:
            self.store.commands[(record.actor_id, record.operation, record.key)] = record


class InMemoryMetricResultReader:
    def __init__(self, results: dict[UUID, TrainingMetricCalculation]) -> None:
        self.results = results

    def list(self, query: MetricResultQuery) -> list[TrainingMetricCalculation]:
        entries = [
            item
            for item in self.results.values()
            if item.athlete_id == query.athlete_id
            and (query.metric_code is None or item.metric_code == query.metric_code)
            and (
                query.after is None
                or (item.measured_at, item.calculation_id)
                < (query.after.measured_at, query.after.calculation_id)
            )
        ]
        entries.sort(key=lambda item: (item.measured_at, item.calculation_id), reverse=True)
        return entries[: query.limit]


def ftp_body(**overrides: Any) -> dict[str, Any]:
    """FTP válido respecto de `NOW` (2026-10-12 12:00 UTC): 250 W → 238 W."""
    values: dict[str, Any] = {
        "calculation_id": str(uuid4()),
        "metric_code": "FTP_ESTIMATED",
        "method_code": "FTP_20_MIN_95_V1",
        "method_version": 1,
        "measured_at": "2026-10-12T11:30:00Z",
        "inputs": [{"code": "mean_power_watts", "value": 250, "unit": "W"}],
    }
    values.update(overrides)
    return values


def cooper_body(**overrides: Any) -> dict[str, Any]:
    """Cooper válido respecto de `NOW`: 2.400 m → 42,4 mL/kg/min."""
    values: dict[str, Any] = {
        "calculation_id": str(uuid4()),
        "metric_code": "VO2MAX_ESTIMATED",
        "method_code": "COOPER_12_MIN_V1",
        "method_version": 1,
        "measured_at": "2026-10-12T11:00:00Z",
        "inputs": [{"code": "distance_meters", "value": 2400, "unit": "m"}],
    }
    values.update(overrides)
    return values


def _heart_rate_recovery(values: Mapping[str, Decimal]) -> Decimal:
    return values["peak_heart_rate_bpm"] - values["recovery_heart_rate_bpm"]


# HA014: tercer método escalar SOLO para pruebas de extensibilidad; nunca llega al producto.
TEST_ONLY_RECOVERY_V1 = MethodDefinition(
    metric_code="HR_RECOVERY_TEST",
    label_key="training_metric.hr_recovery_test",
    result_unit="bpm",
    result_scale=0,
    method_code="HR_RECOVERY_1_MIN_TEST_V1",
    method_version=1,
    method_label_key="training_metric.method.hr_recovery_1_min_test_v1",
    inputs=(
        InputDefinition(
            code="peak_heart_rate_bpm",
            label_key="training_metric.input.peak_heart_rate_bpm",
            unit="bpm",
            minimum=Decimal(60),
            maximum=Decimal(230),
        ),
        InputDefinition(
            code="recovery_heart_rate_bpm",
            label_key="training_metric.input.recovery_heart_rate_bpm",
            unit="bpm",
            minimum=Decimal(40),
            maximum=Decimal(230),
        ),
    ),
    formula=_heart_rate_recovery,
)

EXTENDED_TEST_CATALOG = MetricCatalog(
    catalog_version="training-metrics-test-extension.v1",
    methods=(FTP_20_MIN_95_V1, COOPER_12_MIN_V1, TEST_ONLY_RECOVERY_V1),
)


def recovery_body(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "calculation_id": str(uuid4()),
        "metric_code": "HR_RECOVERY_TEST",
        "method_code": "HR_RECOVERY_1_MIN_TEST_V1",
        "method_version": 1,
        "measured_at": "2026-10-12T11:45:00Z",
        "inputs": [
            {"code": "recovery_heart_rate_bpm", "value": 140, "unit": "bpm"},
            {"code": "peak_heart_rate_bpm", "value": 172, "unit": "bpm"},
        ],
    }
    values.update(overrides)
    return values
