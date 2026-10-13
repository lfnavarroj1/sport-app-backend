"""Dobles en memoria para probar HU032 sin infraestructura."""

from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

from training_management.application.errors import (
    ActiveSessionExists,
    IdempotencyKeyConflict,
    SessionIdConflict,
)
from training_management.application.ports import (
    ActiveSessionView,
    CommandRecord,
    OutboxMessage,
)
from training_management.domain.plans.model import Sport
from training_management.domain.sessions.model import TrainingSession, TrainingSessionStatus


@dataclass(frozen=True)
class StoredPlan:
    athlete_id: UUID
    sports: frozenset[Sport]
    status: str = "ASSIGNED"


@dataclass
class InMemorySessionStore:
    sessions: dict[UUID, TrainingSession] = field(default_factory=dict)
    outbox: list[OutboxMessage] = field(default_factory=list)
    commands: dict[tuple[UUID, str, UUID], CommandRecord] = field(default_factory=dict)
    plans: dict[UUID, StoredPlan] = field(default_factory=dict)


class InMemorySessionUnitOfWork:
    """Aplica los cambios solo al confirmar y valida las mismas restricciones que PostgreSQL."""

    def __init__(
        self, store: InMemorySessionStore, before_commit: Callable[[], None] | None = None
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self._added: list[TrainingSession] = []
        self._updated: list[TrainingSession] = []
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
        self._added, self._updated, self._outbox, self._commands, self._deleted = (
            [],
            [],
            [],
            [],
            [],
        )

    def find_command(self, actor_id: UUID, operation: str, key: UUID) -> CommandRecord | None:
        return self.store.commands.get((actor_id, operation, key))

    def delete_command(self, actor_id: UUID, operation: str, key: UUID) -> None:
        self._deleted.append((actor_id, operation, key))

    def add_command(self, record: CommandRecord) -> None:
        self._commands.append(record)

    def get_session(self, session_id: UUID, *, for_update: bool = False) -> TrainingSession | None:
        return self.store.sessions.get(session_id)

    def plan_allows(self, plan_id: UUID, athlete_id: UUID, sport: Sport) -> bool:
        plan = self.store.plans.get(plan_id)
        return (
            plan is not None
            and plan.athlete_id == athlete_id
            and plan.status == "ASSIGNED"
            and sport in plan.sports
        )

    def add_session(self, session: TrainingSession) -> None:
        self._added.append(session)

    def update_session(self, session: TrainingSession) -> None:
        self._updated.append(session)

    def add_outbox(self, message: OutboxMessage) -> None:
        self._outbox.append(message)

    def commit(self) -> None:
        if self.before_commit is not None:
            hook, self.before_commit = self.before_commit, None
            hook()
        for key in self._deleted:
            self.store.commands.pop(key, None)
        for session in self._added:
            if session.session_id in self.store.sessions:
                raise SessionIdConflict()
            if any(
                other.athlete_id == session.athlete_id
                and other.status is TrainingSessionStatus.ACTIVE
                for other in self.store.sessions.values()
            ):
                raise ActiveSessionExists()
        if any(
            (record.actor_id, record.operation, record.key) in self.store.commands
            for record in self._commands
        ):
            raise IdempotencyKeyConflict()
        for session in [*self._added, *self._updated]:
            self.store.sessions[session.session_id] = session
        self.store.outbox.extend(self._outbox)
        for record in self._commands:
            self.store.commands[(record.actor_id, record.operation, record.key)] = record


class InMemoryActiveSessionReader:
    def __init__(self, active: dict[UUID, ActiveSessionView]) -> None:
        self.active = active

    def current(self, athlete_id: UUID) -> ActiveSessionView | None:
        own = [view for view in self.active.values() if view.athlete_id == athlete_id]
        return max(own, key=lambda view: (view.started_at, view.session_id), default=None)


def start_body(**overrides: Any) -> dict[str, Any]:
    """Inicio válido respecto de `NOW` (2026-10-12 12:00 UTC), sin plan."""
    values: dict[str, Any] = {
        "session_id": str(uuid4()),
        "plan_id": None,
        "sport": "running",
        "started_at": "2026-10-12T11:30:00Z",
    }
    values.update(overrides)
    return values
