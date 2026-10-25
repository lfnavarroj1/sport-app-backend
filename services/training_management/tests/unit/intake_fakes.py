"""Dobles en memoria y datos sintéticos para probar HU043 sin infraestructura."""

from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

from training_management.application.errors import IdempotencyKeyConflict, IntakeIdConflict
from training_management.application.ports import CommandRecord, OutboxMessage
from training_management.domain.nutrition.intake import FoodIntake


@dataclass
class InMemoryFoodIntakeStore:
    intakes: dict[UUID, FoodIntake] = field(default_factory=dict)
    outbox: list[OutboxMessage] = field(default_factory=list)
    commands: dict[tuple[UUID, str, UUID], CommandRecord] = field(default_factory=dict)


class InMemoryFoodIntakeUnitOfWork:
    """Aplica los cambios solo al confirmar y valida las mismas claves primarias que PostgreSQL."""

    def __init__(
        self, store: InMemoryFoodIntakeStore, before_commit: Callable[[], None] | None = None
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self._intakes: list[FoodIntake] = []
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
        self._intakes, self._outbox, self._commands, self._deleted = [], [], [], []

    def find_command(self, actor_id: UUID, operation: str, key: UUID) -> CommandRecord | None:
        return self.store.commands.get((actor_id, operation, key))

    def delete_command(self, actor_id: UUID, operation: str, key: UUID) -> None:
        self._deleted.append((actor_id, operation, key))

    def add_command(self, record: CommandRecord) -> None:
        self._commands.append(record)

    def get_food_intake(self, intake_id: UUID) -> FoodIntake | None:
        return self.store.intakes.get(intake_id)

    def add_food_intake(self, intake: FoodIntake) -> None:
        self._intakes.append(intake)

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
        if any(item.intake_id in self.store.intakes for item in self._intakes):
            raise IntakeIdConflict()
        self.store.intakes.update((item.intake_id, item) for item in self._intakes)
        self.store.outbox.extend(self._outbox)
        for record in self._commands:
            self.store.commands[(record.actor_id, record.operation, record.key)] = record


def intake_item(food_code: str = "OATS", quantity: str = "80", unit: str = "G") -> dict[str, Any]:
    return {"food_code": food_code, "quantity": quantity, "unit": unit}


def intake_body(**overrides: Any) -> dict[str, Any]:
    """Consumo válido respecto de `NOW` (2026-10-12 12:00 UTC) con datos sintéticos."""
    values: dict[str, Any] = {
        "intake_id": str(uuid4()),
        "consumed_at": "2026-10-12T07:30:00Z",
        "meal_type": "BREAKFAST",
        "items": [
            intake_item(),
            intake_item("BANANA", "1", "UNIT"),
            intake_item("MILK", "250.0", "ML"),
        ],
    }
    values.update(overrides)
    return values
