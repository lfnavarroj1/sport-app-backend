"""Base de Afiliación migrada y casos de uso reales para pruebas de integración."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from affiliation_management.application.commands.provision_free_subscription import (
    ProvisionFreeSubscription,
)
from affiliation_management.application.ports import ProvisioningUnitOfWork
from affiliation_management.infrastructure.clock import SystemClock
from affiliation_management.infrastructure.persistence.unit_of_work import (
    SqlAlchemyProvisioningUnitOfWork,
)
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.postgres import temporary_database

HISTORY = "affiliation_management.infrastructure.persistence:migrations"
CATALOG = "plans-2026-10-11.v1"


@dataclass
class AffiliationDatabase:
    url: str
    engine: Engine
    sessions: sessionmaker[Session]

    def provision(
        self, unit_of_work: Callable[[], ProvisioningUnitOfWork] | None = None
    ) -> ProvisionFreeSubscription:
        return ProvisionFreeSubscription(
            unit_of_work=unit_of_work or (lambda: SqlAlchemyProvisioningUnitOfWork(self.sessions)),
            clock=SystemClock(),
        )

    def count(self, model: Any) -> int:
        with self.sessions() as session:
            return session.scalar(select(func.count()).select_from(model)) or 0


@contextmanager
def affiliation_database(prefix: str) -> Iterator[AffiliationDatabase]:
    with temporary_database(prefix) as url:
        upgrade(HISTORY, url)
        engine = create_engine(url, pool_size=10, max_overflow=10)
        try:
            yield AffiliationDatabase(url, engine, sessionmaker(engine, expire_on_commit=False))
        finally:
            engine.dispose()


def user_registered(**overrides: Any) -> dict[str, Any]:
    """Sobre de `usuario.registrado.v1` tal como lo publica Usuarios."""
    user_id = str(overrides.pop("user_id", uuid4()))
    envelope: dict[str, Any] = {
        "event_id": str(uuid4()),
        "event_type": "usuario.registrado.v1",
        "occurred_at": "2026-10-11T12:00:00+00:00",
        "producer": "gestion-usuarios",
        "aggregate_id": user_id,
        "aggregate_version": 1,
        "correlation_id": str(uuid4()),
        "causation_id": str(uuid4()),
        "schema_version": 1,
        "payload": {
            "user_id": user_id,
            "full_name": "Ana Pérez",
            "email": f"ana-{uuid4().hex[:8]}@example.com",
            "actor_type": overrides.pop("actor_type", "athlete"),
            "status": "active",
            "registered_at": "2026-10-11T12:00:00+00:00",
        },
    }
    envelope.update(overrides)
    return envelope
