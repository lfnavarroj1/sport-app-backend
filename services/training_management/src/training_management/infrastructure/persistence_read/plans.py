"""Base de lectura de entrenamientos: consulta de planes (HU010 CA4) y transacción del
proyector (CMP-003) para planes, autorizaciones, historial de sesiones (HU011), sesión
activa (HU032), resultados de indicadores (HU034) y planes de alimentación (HU014)."""

from datetime import date, datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from training_management.application.ports import (
    ActiveSessionView,
    AuthorizationView,
    DuplicateDelivery,
    PlanView,
    SessionHistoryEntry,
    SessionView,
)
from training_management.domain.metrics.model import TrainingMetricCalculation
from training_management.domain.nutrition.model import NutritionPlan
from training_management.domain.plans.events import planned_session_id
from training_management.domain.routes.model import Route
from training_management.domain.templates.model import TrainingPlanTemplate
from training_management.infrastructure.persistence_read.metrics import (
    metric_result,
    metric_result_values,
)
from training_management.infrastructure.persistence_read.models import (
    PLAN_VIEW_PRIMARY_KEY,
    PROCESSED_EVENT_PRIMARY_KEY,
    ActiveTrainingSessionRow,
    NutritionPlanViewRow,
    ProcessedEventRow,
    RouteViewRow,
    TrainingAuthorizationViewRow,
    TrainingMetricResultRow,
    TrainingPlanSessionViewRow,
    TrainingPlanTemplateViewRow,
    TrainingPlanViewRow,
    TrainingSessionHistoryRow,
)
from training_management.infrastructure.persistence_read.nutrition import nutrition_plan_values
from training_management.infrastructure.persistence_read.routes import route_view_values
from training_management.infrastructure.persistence_read.templates import (
    template_view_values,
)


def _sessions_document(view: PlanView) -> list[dict[str, Any]]:
    return [
        {
            "scheduled_date": session.scheduled_date.isoformat(),
            "sport": session.sport,
            "duration_minutes": session.duration_minutes,
            "intensity_rpe": session.intensity_rpe,
        }
        for session in view.sessions
    ]


def _view(row: TrainingPlanViewRow) -> PlanView:
    return PlanView(
        plan_id=row.plan_id,
        athlete_id=row.athlete_id,
        professional_id=row.professional_id,
        title=row.title,
        objectives=tuple(row.objectives),
        start_date=row.start_date,
        end_date=row.end_date,
        status=row.status,
        version=row.version,
        sessions=tuple(
            SessionView(
                scheduled_date=date.fromisoformat(item["scheduled_date"]),
                sport=item["sport"],
                duration_minutes=int(item["duration_minutes"]),
                intensity_rpe=int(item["intensity_rpe"]),
            )
            for item in row.sessions
        ),
        created_at=row.created_at,
    )


def _violated_constraint(error: IntegrityError) -> str | None:
    diagnostics = getattr(error.orig, "diag", None)
    return getattr(diagnostics, "constraint_name", None)


class SqlAlchemyPlanReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get(self, plan_id: UUID) -> PlanView | None:
        with self._session_factory() as session:
            row = session.get(TrainingPlanViewRow, plan_id)
            return _view(row) if row is not None else None


class SqlAlchemyProjectionUnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None

    @property
    def session(self) -> Session:
        if self._session is None:
            raise RuntimeError("La unidad de trabajo no está abierta.")
        return self._session

    def __enter__(self) -> Self:
        self._session = self._session_factory()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.session.rollback()
        self.session.close()
        self._session = None

    def is_processed(self, event_id: UUID) -> bool:
        return self.session.get(ProcessedEventRow, event_id) is not None

    def projected_version(self, plan_id: UUID) -> int | None:
        # Bloquea la vista existente: dos versiones del mismo plan se aplican en serie.
        version: int | None = self.session.scalar(
            select(TrainingPlanViewRow.version)
            .where(TrainingPlanViewRow.plan_id == plan_id)
            .with_for_update()
        )
        return version

    def add_plan_view(self, view: PlanView, projected_at: datetime) -> None:
        self.session.add(
            TrainingPlanViewRow(
                plan_id=view.plan_id,
                athlete_id=view.athlete_id,
                professional_id=view.professional_id,
                title=view.title,
                objectives=list(view.objectives),
                start_date=view.start_date,
                end_date=view.end_date,
                status=view.status,
                version=view.version,
                sessions=_sessions_document(view),
                created_at=view.created_at,
                projected_at=projected_at,
            )
        )

    def save_planned_sessions(self, view: PlanView, projected_at: datetime) -> None:
        rows = [
            {
                "session_id": planned_session_id(view.plan_id, position),
                "plan_id": view.plan_id,
                "athlete_id": view.athlete_id,
                "plan_title": view.title,
                "plan_status": view.status,
                "plan_version": view.version,
                "position": position,
                "scheduled_date": session.scheduled_date,
                "sport": session.sport,
                "duration_minutes": session.duration_minutes,
                "intensity_rpe": session.intensity_rpe,
                "projected_at": projected_at,
            }
            for position, session in enumerate(view.sessions)
        ]
        if not rows:
            return
        statement = insert(TrainingPlanSessionViewRow).values(rows)
        # Sin autoflush: la vista del plan sigue pendiente hasta `commit`, donde una entrega
        # concurrente del mismo evento se traduce a `DuplicateDelivery`.
        with self.session.no_autoflush:
            self.session.execute(
                statement.on_conflict_do_update(
                    index_elements=["session_id"],
                    set_={key: statement.excluded[key] for key in rows[0] if key != "session_id"},
                )
            )

    def authorization_version(self, athlete_id: UUID, professional_id: UUID) -> int | None:
        version: int | None = self.session.scalar(
            select(TrainingAuthorizationViewRow.version)
            .where(
                TrainingAuthorizationViewRow.athlete_id == athlete_id,
                TrainingAuthorizationViewRow.professional_id == professional_id,
            )
            .with_for_update()
        )
        return version

    def save_authorization_view(self, view: AuthorizationView, projected_at: datetime) -> None:
        values = {
            "athlete_id": view.athlete_id,
            "professional_id": view.professional_id,
            "status": view.status,
            "version": view.version,
            "granted_at": view.granted_at,
            "revoked_at": view.revoked_at,
            "projected_at": projected_at,
        }
        statement = insert(TrainingAuthorizationViewRow).values(**values)
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=["athlete_id", "professional_id"],
                set_={
                    key: statement.excluded[key]
                    for key in values
                    if key not in ("athlete_id", "professional_id")
                },
            )
        )

    def session_version(self, session_id: UUID) -> int | None:
        version: int | None = self.session.scalar(
            select(TrainingSessionHistoryRow.version)
            .where(TrainingSessionHistoryRow.session_id == session_id)
            .with_for_update()
        )
        return version

    def save_session_entry(self, entry: SessionHistoryEntry, projected_at: datetime) -> None:
        values = {
            "session_id": entry.session_id,
            "athlete_id": entry.athlete_id,
            "plan_id": entry.plan_id,
            "sport": entry.sport,
            "started_at": entry.started_at,
            "ended_at": entry.ended_at,
            "duration_seconds": entry.duration_seconds,
            "version": entry.version,
            "projected_at": projected_at,
        }
        statement = insert(TrainingSessionHistoryRow).values(**values)
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=["session_id"],
                set_={key: statement.excluded[key] for key in values if key != "session_id"},
            )
        )

    def active_session_version(self, session_id: UUID) -> int | None:
        version: int | None = self.session.scalar(
            select(ActiveTrainingSessionRow.version)
            .where(ActiveTrainingSessionRow.session_id == session_id)
            .with_for_update()
        )
        return version

    def save_active_session(self, view: ActiveSessionView, projected_at: datetime) -> None:
        values = {
            "session_id": view.session_id,
            "athlete_id": view.athlete_id,
            "plan_id": view.plan_id,
            "sport": view.sport,
            "started_at": view.started_at,
            "version": view.version,
            "projected_at": projected_at,
        }
        statement = insert(ActiveTrainingSessionRow).values(**values)
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=["session_id"],
                set_={key: statement.excluded[key] for key in values if key != "session_id"},
            )
        )

    def delete_active_session(self, session_id: UUID) -> None:
        self.session.execute(
            delete(ActiveTrainingSessionRow).where(
                ActiveTrainingSessionRow.session_id == session_id
            )
        )

    def metric_result(self, calculation_id: UUID) -> TrainingMetricCalculation | None:
        row = self.session.scalar(
            select(TrainingMetricResultRow)
            .where(TrainingMetricResultRow.calculation_id == calculation_id)
            .with_for_update()
        )
        return metric_result(row) if row is not None else None

    def add_metric_result(
        self, calculation: TrainingMetricCalculation, projected_at: datetime
    ) -> None:
        self.session.add(TrainingMetricResultRow(**metric_result_values(calculation, projected_at)))

    def nutrition_plan_version(self, plan_id: UUID) -> int | None:
        version: int | None = self.session.scalar(
            select(NutritionPlanViewRow.version)
            .where(NutritionPlanViewRow.plan_id == plan_id)
            .with_for_update()
        )
        return version

    def save_nutrition_plan_view(self, plan: NutritionPlan, projected_at: datetime) -> None:
        values = nutrition_plan_values(plan, projected_at)
        statement = insert(NutritionPlanViewRow).values(**values)
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=["plan_id"],
                set_={key: statement.excluded[key] for key in values if key != "plan_id"},
            )
        )

    def route_version(self, route_id: UUID) -> int | None:
        version: int | None = self.session.scalar(
            select(RouteViewRow.version).where(RouteViewRow.route_id == route_id).with_for_update()
        )
        return version

    def save_route_view(self, route: Route, projected_at: datetime) -> None:
        values = route_view_values(route, projected_at)
        statement = insert(RouteViewRow).values(**values)
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=["route_id"],
                set_={key: statement.excluded[key] for key in values if key != "route_id"},
            )
        )

    def template_version(self, template_id: UUID) -> int | None:
        version: int | None = self.session.scalar(
            select(TrainingPlanTemplateViewRow.version)
            .where(TrainingPlanTemplateViewRow.template_id == template_id)
            .with_for_update()
        )
        return version

    def save_template_view(self, template: TrainingPlanTemplate, projected_at: datetime) -> None:
        values = template_view_values(template, projected_at)
        statement = insert(TrainingPlanTemplateViewRow).values(**values)
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=["template_id"],
                set_={key: statement.excluded[key] for key in values if key != "template_id"},
            )
        )

    def add_processed(
        self, event_id: UUID, event_type: str, aggregate_id: UUID, processed_at: datetime
    ) -> None:
        self.session.add(
            ProcessedEventRow(
                event_id=event_id,
                event_type=event_type,
                aggregate_id=aggregate_id,
                processed_at=processed_at,
            )
        )

    def commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            # Otra entrega del mismo evento (o del mismo plan nuevo) confirmó primero.
            if _violated_constraint(error) in {PROCESSED_EVENT_PRIMARY_KEY, PLAN_VIEW_PRIMARY_KEY}:
                raise DuplicateDelivery() from None
            raise
