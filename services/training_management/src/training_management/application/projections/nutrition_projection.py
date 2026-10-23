"""HU014: proyectar `plan.alimentacion_creado.v1` en `nutrition_plan_view` (ADR-037, CMP-003).

El evento trae el estado completo del plan. En una sola transacción de la base de lectura:
1. si el `event_id` ya está en `processed_event`, es una entrega repetida (sin efectos);
2. si el plan no está proyectado, se inserta; si la versión del evento es mayor que la
   proyectada, se reemplaza el estado completo;
3. si la vista ya tiene esa versión o una posterior (p. ej., al reconstruir), el evento
   está superado y solo se registra como procesado.
Un evento que no cumple el contrato no llega aquí: `application.messages` lo rechaza y,
agotados los reintentos, termina en la DLQ.
"""

from collections.abc import Callable

from training_management.application.messages import NutritionPlanCreatedEvent
from training_management.application.ports import Clock, DuplicateDelivery, ProjectionUnitOfWork
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.domain.nutrition.events import EVENT_TYPE


class ProjectNutritionPlanCreated:
    def __init__(self, *, unit_of_work: Callable[[], ProjectionUnitOfWork], clock: Clock) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(self, event: NutritionPlanCreatedEvent) -> ProjectionOutcome:
        plan = event.plan
        now = self._clock.now()
        try:
            with self._unit_of_work() as unit_of_work:
                if unit_of_work.is_processed(event.event_id):
                    return ProjectionOutcome.DUPLICATE
                projected = unit_of_work.nutrition_plan_version(plan.plan_id)
                if projected is not None and projected >= plan.version:
                    outcome = ProjectionOutcome.STALE
                else:
                    unit_of_work.save_nutrition_plan_view(plan, now)
                    outcome = ProjectionOutcome.PROJECTED
                unit_of_work.add_processed(event.event_id, EVENT_TYPE, plan.plan_id, now)
                unit_of_work.commit()
        except DuplicateDelivery:
            return ProjectionOutcome.DUPLICATE
        return outcome
