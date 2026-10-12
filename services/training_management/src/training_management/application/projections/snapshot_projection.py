"""Proyecciones de HU011 a partir de eventos con el estado completo (snapshot).

`autorizacion_entrenamiento.otorgada.v1` / `.revocada.v1` y `entrenamiento.finalizado.v1`
traen el estado completo del agregado, así que no hace falta exigir la versión
siguiente: en una sola transacción de lectura se aplica el evento solo si su versión
es mayor que la proyectada; uno igual o anterior queda registrado como superado. Así
un duplicado, un evento atrasado o el backfill de autorizaciones nunca retroceden la
vista y no bloquean la secuencia.
"""

from collections.abc import Callable

from training_management.application.messages import (
    AuthorizationChangedEvent,
    PlanCreatedEvent,
    SessionFinishedEvent,
    TrainingEvent,
)
from training_management.application.ports import Clock, DuplicateDelivery, ProjectionUnitOfWork
from training_management.application.projections.plan_projection import (
    ProjectionOutcome,
    ProjectPlanCreated,
)
from training_management.domain.sessions.events import EVENT_TYPE as SESSION_FINISHED


class ProjectAuthorizationChanged:
    def __init__(self, *, unit_of_work: Callable[[], ProjectionUnitOfWork], clock: Clock) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(self, event: AuthorizationChangedEvent) -> ProjectionOutcome:
        view = event.authorization
        now = self._clock.now()
        try:
            with self._unit_of_work() as unit_of_work:
                if unit_of_work.is_processed(event.event_id):
                    return ProjectionOutcome.DUPLICATE
                projected = unit_of_work.authorization_version(
                    view.athlete_id, view.professional_id
                )
                if projected is not None and projected >= view.version:
                    outcome = ProjectionOutcome.STALE
                else:
                    unit_of_work.save_authorization_view(view, now)
                    outcome = ProjectionOutcome.PROJECTED
                unit_of_work.add_processed(
                    event.event_id, event.event_type, event.aggregate_id, now
                )
                unit_of_work.commit()
        except DuplicateDelivery:
            return ProjectionOutcome.DUPLICATE
        return outcome


class ProjectSessionFinished:
    def __init__(self, *, unit_of_work: Callable[[], ProjectionUnitOfWork], clock: Clock) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(self, event: SessionFinishedEvent) -> ProjectionOutcome:
        entry = event.session
        now = self._clock.now()
        try:
            with self._unit_of_work() as unit_of_work:
                if unit_of_work.is_processed(event.event_id):
                    return ProjectionOutcome.DUPLICATE
                projected = unit_of_work.session_version(entry.session_id)
                if projected is not None and projected >= entry.version:
                    outcome = ProjectionOutcome.STALE
                else:
                    unit_of_work.save_session_entry(entry, now)
                    outcome = ProjectionOutcome.PROJECTED
                unit_of_work.add_processed(event.event_id, SESSION_FINISHED, entry.session_id, now)
                unit_of_work.commit()
        except DuplicateDelivery:
            return ProjectionOutcome.DUPLICATE
        return outcome


class TrainingProjector:
    """Despacha cada evento validado a su proyección."""

    def __init__(self, *, unit_of_work: Callable[[], ProjectionUnitOfWork], clock: Clock) -> None:
        self._plans = ProjectPlanCreated(unit_of_work=unit_of_work, clock=clock)
        self._authorizations = ProjectAuthorizationChanged(unit_of_work=unit_of_work, clock=clock)
        self._sessions = ProjectSessionFinished(unit_of_work=unit_of_work, clock=clock)

    def __call__(self, event: TrainingEvent) -> ProjectionOutcome:
        if isinstance(event, PlanCreatedEvent):
            return self._plans(event)
        if isinstance(event, AuthorizationChangedEvent):
            return self._authorizations(event)
        return self._sessions(event)
