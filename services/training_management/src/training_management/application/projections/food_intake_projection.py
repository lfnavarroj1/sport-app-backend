"""HU043: proyectar `consumo_alimento.registrado.v1` en `food_intake_view` (ADR-037, CMP-003).

El evento trae el estado completo del consumo. En una sola transacción de la base de lectura:
1. si el `event_id` ya está en `processed_event`, es una entrega repetida (sin efectos);
2. si el consumo no está proyectado, se inserta; si la versión del evento es mayor que la
   proyectada, se reemplaza el estado completo;
3. si la vista ya tiene esa versión o una posterior (p. ej., al reconstruir), el evento
   está superado y solo se registra como procesado.
Un evento que no cumple el contrato no llega aquí: `application.messages` lo rechaza y,
agotados los reintentos, termina en la DLQ.
"""

from collections.abc import Callable

from training_management.application.messages import FoodIntakeRegisteredEvent
from training_management.application.ports import Clock, DuplicateDelivery, ProjectionUnitOfWork
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.domain.nutrition.intake_events import EVENT_TYPE


class ProjectFoodIntakeRegistered:
    def __init__(self, *, unit_of_work: Callable[[], ProjectionUnitOfWork], clock: Clock) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(self, event: FoodIntakeRegisteredEvent) -> ProjectionOutcome:
        intake = event.intake
        now = self._clock.now()
        try:
            with self._unit_of_work() as unit_of_work:
                if unit_of_work.is_processed(event.event_id):
                    return ProjectionOutcome.DUPLICATE
                projected = unit_of_work.food_intake_version(intake.intake_id)
                if projected is not None and projected >= intake.version:
                    outcome = ProjectionOutcome.STALE
                else:
                    unit_of_work.save_food_intake_view(intake, now)
                    outcome = ProjectionOutcome.PROJECTED
                unit_of_work.add_processed(event.event_id, EVENT_TYPE, intake.intake_id, now)
                unit_of_work.commit()
        except DuplicateDelivery:
            return ProjectionOutcome.DUPLICATE
        return outcome
