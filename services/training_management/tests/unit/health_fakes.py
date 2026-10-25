"""Dobles en memoria y datos sintéticos para probar HU042 sin infraestructura."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

from fakes import NOW

from training_management.application.errors import IdempotencyKeyConflict, ReportIdConflict
from training_management.application.ports import CommandRecord, OutboxMessage
from training_management.domain.health.events import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    registered_payload,
)
from training_management.domain.health.model import HealthReport, validate_report
from training_management.domain.plans.model import Sport
from training_management.domain.sessions.model import TrainingSession, TrainingSessionStatus
from training_management.infrastructure.persistence_write.outbox import ClaimedEvent
from training_management.workers.outbox_publisher import build_envelope

CONSENT = "local-dev"


@dataclass
class InMemoryHealthStore:
    reports: dict[UUID, HealthReport] = field(default_factory=dict)
    outbox: list[OutboxMessage] = field(default_factory=list)
    commands: dict[tuple[UUID, str, UUID], CommandRecord] = field(default_factory=dict)
    sessions: dict[UUID, TrainingSession] = field(default_factory=dict)


class InMemoryHealthReportUnitOfWork:
    """Aplica los cambios solo al confirmar y valida las mismas claves primarias que PostgreSQL."""

    def __init__(
        self, store: InMemoryHealthStore, before_commit: Callable[[], None] | None = None
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self._reports: list[HealthReport] = []
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
        self._reports, self._outbox, self._commands, self._deleted = [], [], [], []

    def find_command(self, actor_id: UUID, operation: str, key: UUID) -> CommandRecord | None:
        return self.store.commands.get((actor_id, operation, key))

    def delete_command(self, actor_id: UUID, operation: str, key: UUID) -> None:
        self._deleted.append((actor_id, operation, key))

    def add_command(self, record: CommandRecord) -> None:
        self._commands.append(record)

    def get_session(self, session_id: UUID) -> TrainingSession | None:
        return self.store.sessions.get(session_id)

    def get_health_report(self, report_id: UUID) -> HealthReport | None:
        return self.store.reports.get(report_id)

    def add_health_report(self, report: HealthReport) -> None:
        self._reports.append(report)

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
        if any(item.report_id in self.store.reports for item in self._reports):
            raise ReportIdConflict()
        for report in self._reports:
            self.store.reports[report.report_id] = report
        self.store.outbox.extend(self._outbox)
        for record in self._commands:
            self.store.commands[(record.actor_id, record.operation, record.key)] = record


def training_session(athlete_id: UUID, session_id: UUID | None = None) -> TrainingSession:
    """Sesión terminada sintética del deportista."""
    started = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
    ended = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
    return TrainingSession(
        session_id=session_id or uuid4(),
        athlete_id=athlete_id,
        plan_id=None,
        sport=Sport("running"),
        started_at=started,
        ended_at=ended,
        duration_seconds=3600,
        status=TrainingSessionStatus.FINISHED,
        version=2,
        created_at=started,
        updated_at=ended,
    )


def health_body(**overrides: Any) -> dict[str, Any]:
    """Reporte válido respecto de `NOW` (2026-10-12 12:00 UTC)."""
    values: dict[str, Any] = {
        "report_id": str(uuid4()),
        "type": "DISCOMFORT",
        "body_area": "KNEE",
        "severity": "MODERATE",
        "occurred_at": "2026-10-12T10:30:00Z",
        "expected_recovery_days": 3,
        "consent": {"version": CONSENT},
    }
    values.update(overrides)
    return values


def a_report(athlete_id: UUID | None = None, **overrides: Any) -> HealthReport:
    data = validate_report(health_body(**overrides), NOW, CONSENT)
    return HealthReport.register(athlete_id=athlete_id or uuid4(), data=data, now=NOW)


def health_envelope(report: HealthReport, **overrides: Any) -> dict[str, Any]:
    """Sobre de `reporte_salud.registrado.v1` tal como lo publica el servicio (ida y vuelta
    por JSON, como viaja por Pub/Sub)."""
    envelope = build_envelope(
        ClaimedEvent(
            event_id=uuid4(),
            event_type=EVENT_TYPE,
            schema_version=SCHEMA_VERSION,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=report.report_id,
            aggregate_version=report.version,
            occurred_at=report.reported_at,
            correlation_id=uuid4(),
            causation_id=str(uuid4()),
            payload=registered_payload(report),
            attempts=0,
        )
    )
    loaded: dict[str, Any] = json.loads(json.dumps(envelope))
    loaded.update(overrides)
    return loaded
