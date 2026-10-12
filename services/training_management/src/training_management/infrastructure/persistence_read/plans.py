"""Base de lectura de planes: consulta (CA4) y transacción del proyector (CMP-003)."""

from datetime import date, datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from training_management.application.ports import DuplicateDelivery, PlanView, SessionView
from training_management.infrastructure.persistence_read.models import (
    PLAN_VIEW_PRIMARY_KEY,
    PROCESSED_EVENT_PRIMARY_KEY,
    ProcessedEventRow,
    TrainingPlanViewRow,
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
