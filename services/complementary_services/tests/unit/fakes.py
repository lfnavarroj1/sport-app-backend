"""Dobles en memoria para probar HU015 sin infraestructura."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from complementary_services.application.errors import IdempotencyKeyConflict
from complementary_services.application.ports import IdempotencyRecord
from complementary_services.domain.catalog.offer import ServiceOffer

NOW = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)


class FixedClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


class FakePermissions:
    def allows(self, actor_type: str, permission: str) -> bool:
        return actor_type == "service_provider" and permission == "services.create"


@dataclass
class InMemoryStore:
    services: dict[UUID, ServiceOffer] = field(default_factory=dict)
    idempotency: dict[tuple[UUID, UUID], IdempotencyRecord] = field(default_factory=dict)


class InMemoryCreationUnitOfWork:
    """Aplica los cambios solo al confirmar y valida la misma unicidad que PostgreSQL."""

    def __init__(
        self, store: InMemoryStore, before_commit: Callable[[], None] | None = None
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self._services: list[ServiceOffer] = []
        self._records: list[IdempotencyRecord] = []
        self._deleted: list[tuple[UUID, UUID]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._services, self._records, self._deleted = [], [], []

    def find_idempotency(self, provider_id: UUID, key: UUID) -> IdempotencyRecord | None:
        return self.store.idempotency.get((provider_id, key))

    def delete_idempotency(self, provider_id: UUID, key: UUID) -> None:
        self._deleted.append((provider_id, key))

    def add_service(self, offer: ServiceOffer) -> None:
        self._services.append(offer)

    def add_idempotency(self, record: IdempotencyRecord) -> None:
        self._records.append(record)

    def commit(self) -> None:
        if self.before_commit is not None:
            hook, self.before_commit = self.before_commit, None
            hook()
        for key in self._deleted:
            self.store.idempotency.pop(key, None)
        if any((r.provider_id, r.key) in self.store.idempotency for r in self._records):
            raise IdempotencyKeyConflict()
        for offer in self._services:
            self.store.services[offer.service_id] = offer
        for record in self._records:
            self.store.idempotency[(record.provider_id, record.key)] = record


def interval(**overrides: Any) -> dict[str, Any]:
    """Intervalo válido en hora de Bogotá respecto de `NOW` (2026-10-12 15:00 UTC)."""
    values: dict[str, Any] = {
        "starts_at": "2026-10-20T09:00:00-05:00",
        "ends_at": "2026-10-20T10:00:00-05:00",
        "time_zone": "America/Bogota",
        "capacity": 1,
    }
    values.update(overrides)
    return values


def service_body(**overrides: Any) -> dict[str, Any]:
    """Cuerpo válido de `POST /v1/services` (ejemplo de ADR-027) con datos sintéticos."""
    values: dict[str, Any] = {
        "name": "Valoración deportiva inicial",
        "description": "Valoración presencial para orientar el plan deportivo.",
        "category": "SPORTS_MEDICINE",
        "modality": "IN_PERSON",
        "price": {"amount": "120000.00", "currency": "COP", "billing_unit": "SESSION"},
        "availability": [interval()],
    }
    values.update(overrides)
    return values
