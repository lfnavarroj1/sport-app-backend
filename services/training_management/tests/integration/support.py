"""Bases de escritura y lectura de Entrenamientos migradas, y casos de uso reales."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.postgres import temporary_database
from training_management.application.commands.authorizations import (
    GrantAuthorization,
    RevokeAuthorization,
)
from training_management.application.commands.calculate_training_metric import (
    CalculateTrainingMetric,
    CalculateTrainingMetricCommand,
)
from training_management.application.commands.create_nutrition_plan import (
    CreateNutritionPlan,
    CreateNutritionPlanCommand,
    ItemInput,
    MealInput,
)
from training_management.application.commands.create_training_plan import (
    CreateTrainingPlan,
    CreateTrainingPlanCommand,
    SessionInput,
)
from training_management.application.commands.training_sessions import (
    FinishTrainingSession,
    FinishTrainingSessionCommand,
    StartTrainingSession,
    StartTrainingSessionCommand,
)
from training_management.application.errors import NutritionRestrictionsUnavailable
from training_management.application.ports import Caller
from training_management.application.projections.snapshot_projection import TrainingProjector
from training_management.application.queries.training_history import GetTrainingHistory
from training_management.application.queries.training_metrics import ListTrainingMetricResults
from training_management.domain.metrics.catalog import TRAINING_METRICS_CATALOG
from training_management.domain.nutrition.model import MealType, NutritionRestrictions
from training_management.domain.plans.model import Sport
from training_management.domain.sessions.model import StartInput
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.persistence_read.history import (
    SqlAlchemyActiveSessionReader,
    SqlAlchemyAuthorizationReader,
    SqlAlchemyHistoryReader,
)
from training_management.infrastructure.persistence_read.metrics import (
    SqlAlchemyMetricResultReader,
)
from training_management.infrastructure.persistence_read.nutrition import (
    SqlAlchemyNutritionPlanReader,
)
from training_management.infrastructure.persistence_read.plans import (
    SqlAlchemyPlanReader,
    SqlAlchemyProjectionUnitOfWork,
)
from training_management.infrastructure.persistence_write.food_catalog import (
    SqlAlchemyFoodCatalogReader,
)
from training_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyAuthorizationUnitOfWork,
    SqlAlchemyMetricUnitOfWork,
    SqlAlchemyNutritionUnitOfWork,
    SqlAlchemyPlanningUnitOfWork,
    SqlAlchemySessionUnitOfWork,
)

WRITE_HISTORY = "training_management.infrastructure.persistence_write:migrations"
READ_HISTORY = "training_management.infrastructure.persistence_read:migrations"


class AlwaysCapable:
    """El deportista tiene `training.plan.manage` (Afiliación se prueba en su servicio)."""

    def has_capability(self, user_id: UUID, capability: str) -> bool:
        return True


class HistoryViewer:
    def allows(self, actor_type: str, permission: str) -> bool:
        return actor_type == "service_provider" and permission == "training.history.view_assigned"


class AuthorOnly:
    def allows(self, actor_type: str, permission: str) -> bool:
        return actor_type == "service_provider" and permission == "training.plan.author"


class NutritionAuthorOnly:
    def allows(self, actor_type: str, permission: str) -> bool:
        return actor_type == "service_provider" and permission == "nutrition.plan.author"


class StaticRestrictions:
    """Respuesta de Usuarios (ADR-035) sustituida en la prueba; por defecto, sin hábitos."""

    def __init__(self, result: NutritionRestrictions | None = None) -> None:
        self.result = result or NutritionRestrictions((), (), None, None)
        self.unavailable = False
        self.calls: list[UUID] = []

    def get(self, athlete_id: UUID) -> NutritionRestrictions:
        self.calls.append(athlete_id)
        if self.unavailable:
            raise NutritionRestrictionsUnavailable()
        return self.result


@dataclass
class TrainingDatabases:
    write_url: str
    read_url: str
    write_engine: Engine
    read_engine: Engine

    @property
    def writes(self) -> sessionmaker[Session]:
        return sessionmaker(self.write_engine, expire_on_commit=False)

    @property
    def reads(self) -> sessionmaker[Session]:
        return sessionmaker(self.read_engine, expire_on_commit=False)

    def create_plan(self) -> CreateTrainingPlan:
        return CreateTrainingPlan(
            unit_of_work=lambda: SqlAlchemyPlanningUnitOfWork(self.writes),
            permissions=AuthorOnly(),
            clock=SystemClock(),
        )

    def grant(self) -> GrantAuthorization:
        return GrantAuthorization(
            unit_of_work=lambda: SqlAlchemyAuthorizationUnitOfWork(self.writes),
            capabilities=AlwaysCapable(),
            clock=SystemClock(),
        )

    def revoke(self) -> RevokeAuthorization:
        return RevokeAuthorization(
            unit_of_work=lambda: SqlAlchemyAuthorizationUnitOfWork(self.writes),
            clock=SystemClock(),
        )

    def project(self) -> TrainingProjector:
        return TrainingProjector(
            unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(self.reads), clock=SystemClock()
        )

    def history(self) -> GetTrainingHistory:
        return GetTrainingHistory(
            reader=SqlAlchemyHistoryReader(self.reads),
            authorizations=SqlAlchemyAuthorizationReader(self.reads),
            permissions=HistoryViewer(),
        )

    def start_session(self) -> StartTrainingSession:
        return StartTrainingSession(
            unit_of_work=lambda: SqlAlchemySessionUnitOfWork(self.writes),
            capabilities=AlwaysCapable(),
            clock=SystemClock(),
        )

    def finish_session(self) -> FinishTrainingSession:
        return FinishTrainingSession(
            unit_of_work=lambda: SqlAlchemySessionUnitOfWork(self.writes), clock=SystemClock()
        )

    def calculate_metric(self) -> CalculateTrainingMetric:
        return CalculateTrainingMetric(
            unit_of_work=lambda: SqlAlchemyMetricUnitOfWork(self.writes),
            capabilities=AlwaysCapable(),
            catalog=TRAINING_METRICS_CATALOG,
            clock=SystemClock(),
        )

    def metric_results(self) -> ListTrainingMetricResults:
        return ListTrainingMetricResults(
            reader=SqlAlchemyMetricResultReader(self.reads),
            capabilities=AlwaysCapable(),
            catalog=TRAINING_METRICS_CATALOG,
        )

    def active_reader(self) -> SqlAlchemyActiveSessionReader:
        return SqlAlchemyActiveSessionReader(self.reads)

    def reader(self) -> SqlAlchemyPlanReader:
        return SqlAlchemyPlanReader(self.reads)

    def create_nutrition_plan(self, restrictions: "StaticRestrictions") -> CreateNutritionPlan:
        return CreateNutritionPlan(
            unit_of_work=lambda: SqlAlchemyNutritionUnitOfWork(self.writes),
            permissions=NutritionAuthorOnly(),
            catalog=SqlAlchemyFoodCatalogReader(self.writes),
            restrictions=restrictions,
            clock=SystemClock(),
        )

    def nutrition_reader(self) -> SqlAlchemyNutritionPlanReader:
        return SqlAlchemyNutritionPlanReader(self.reads)

    def discard_pending_outbox(self) -> None:
        """Marca como publicados los eventos pendientes (p. ej., los de autorizaciones de HU011)
        cuando la prueba se centra en los eventos posteriores."""
        with self.writes() as session, session.begin():
            session.execute(text("UPDATE outbox_event SET published_at = now()"))

    def count(self, model: Any, *, read: bool = False) -> int:
        with (self.reads if read else self.writes)() as session:
            return session.scalar(select(func.count()).select_from(model)) or 0


@contextmanager
def training_databases(prefix: str) -> Iterator[TrainingDatabases]:
    with (
        temporary_database(f"{prefix}_write") as write_url,
        temporary_database(f"{prefix}_read") as read_url,
    ):
        upgrade(WRITE_HISTORY, write_url)
        upgrade(READ_HISTORY, read_url)
        write_engine = create_engine(write_url, pool_size=10, max_overflow=10)
        read_engine = create_engine(read_url, pool_size=10, max_overflow=10)
        try:
            yield TrainingDatabases(write_url, read_url, write_engine, read_engine)
        finally:
            write_engine.dispose()
            read_engine.dispose()


def athlete_and_professional() -> tuple[Caller, Caller]:
    return Caller(uuid4(), "athlete"), Caller(uuid4(), "service_provider")


def plan_command(athlete_id: UUID, **overrides: Any) -> CreateTrainingPlanCommand:
    values: dict[str, Any] = {
        "athlete_id": athlete_id,
        "title": "Base aeróbica",
        "objectives": ("Correr 10 km",),
        "start_date": date(2026, 11, 2),
        "end_date": date(2026, 11, 29),
        "sessions": (
            SessionInput(date(2026, 11, 2), Sport.RUNNING, 40, 5),
            SessionInput(date(2026, 11, 4), Sport.STRENGTH, 30, 6),
        ),
        "idempotency_key": uuid4(),
        "correlation_id": uuid4(),
    }
    values.update(overrides)
    return CreateTrainingPlanCommand(**values)


def utc_seconds(delta: timedelta = timedelta()) -> str:
    """Instante de ADR-029 (UTC, `Z`, segundos) relativo al reloj real."""
    instant = (datetime.now(UTC) + delta).replace(microsecond=0)
    return instant.isoformat().replace("+00:00", "Z")


def start_command(
    session_id: UUID | None = None,
    *,
    key: UUID | None = None,
    plan_id: UUID | None = None,
    sport: str = "running",
    started_at: str | None = None,
) -> StartTrainingSessionCommand:
    return StartTrainingSessionCommand(
        start=StartInput(
            session_id=str(session_id or uuid4()),
            plan_id=str(plan_id) if plan_id else None,
            sport=sport,
            started_at=started_at or utc_seconds(-timedelta(minutes=40)),
        ),
        idempotency_key=key or uuid4(),
        correlation_id=uuid4(),
    )


def finish_command(
    session_id: UUID, *, key: UUID | None = None, ended_at: str | None = None
) -> FinishTrainingSessionCommand:
    return FinishTrainingSessionCommand(
        session_id=str(session_id),
        ended_at=ended_at or utc_seconds(-timedelta(minutes=5)),
        idempotency_key=key or uuid4(),
        correlation_id=uuid4(),
    )


def metric_command(
    *,
    key: UUID | None = None,
    calculation_id: UUID | None = None,
    watts: int = 250,
    measured_at: str | None = None,
) -> CalculateTrainingMetricCommand:
    """FTP de ADR-030 (UTC, `Z`, segundos) relativo al reloj real."""
    return CalculateTrainingMetricCommand(
        body={
            "calculation_id": str(calculation_id or uuid4()),
            "metric_code": "FTP_ESTIMATED",
            "method_code": "FTP_20_MIN_95_V1",
            "method_version": 1,
            "measured_at": measured_at or utc_seconds(-timedelta(hours=1)),
            "inputs": [{"code": "mean_power_watts", "value": watts, "unit": "W"}],
        },
        idempotency_key=str(key or uuid4()),
        correlation_id=uuid4(),
    )


def nutrition_command(athlete_id: UUID, **overrides: Any) -> CreateNutritionPlanCommand:
    """Plan de alimentación de HU014 con datos sintéticos."""
    values: dict[str, Any] = {
        "athlete_id": athlete_id,
        "title": "Plan de recuperación",
        "objectives": ("Hidratación diaria",),
        "start_date": date(2026, 11, 2),
        "end_date": date(2026, 11, 29),
        "meals": (
            MealInput(
                MealType.BREAKFAST,
                (ItemInput("OATS", Decimal("80"), "G"), ItemInput("MILK", Decimal("200"), "ML")),
            ),
            MealInput(MealType.LUNCH, (ItemInput("CHICKEN_BREAST", Decimal("150.5"), "G"),)),
        ),
        "idempotency_key": uuid4(),
        "correlation_id": uuid4(),
    }
    values.update(overrides)
    return CreateNutritionPlanCommand(**values)
