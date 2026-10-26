"""HU030: proyectar `plantilla_rutina.publicada.v1` / `.retirada.v1` en
`nutrition_routine_template_view` (ADR-041, CMP-003).

Mismo criterio que las plantillas de HU028: deduplicación por `event_id`, inserción si falta y
reemplazo del estado completo solo con una versión mayor (un retiro que llega antes que su
publicación no se deshace). Un evento fuera de contrato lo rechaza `application.messages`.
"""

from collections.abc import Callable

from training_management.application.messages import RoutineChangedEvent
from training_management.application.ports import Clock, DuplicateDelivery, ProjectionUnitOfWork
from training_management.application.projections.plan_projection import ProjectionOutcome


class ProjectRoutineChanged:
    def __init__(self, *, unit_of_work: Callable[[], ProjectionUnitOfWork], clock: Clock) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(self, event: RoutineChangedEvent) -> ProjectionOutcome:
        routine = event.routine
        now = self._clock.now()
        try:
            with self._unit_of_work() as unit_of_work:
                if unit_of_work.is_processed(event.event_id):
                    return ProjectionOutcome.DUPLICATE
                projected = unit_of_work.routine_version(routine.template_id)
                if projected is not None and projected >= routine.version:
                    outcome = ProjectionOutcome.STALE
                else:
                    unit_of_work.save_routine_view(routine, now)
                    outcome = ProjectionOutcome.PROJECTED
                unit_of_work.add_processed(
                    event.event_id, event.event_type, routine.template_id, now
                )
                unit_of_work.commit()
        except DuplicateDelivery:
            return ProjectionOutcome.DUPLICATE
        return outcome
