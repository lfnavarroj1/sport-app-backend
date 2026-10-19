"""`entrenamientos-read-projector`: proyectar `plan.entrenamiento_creado.v1` (CMP-003).

En una sola transacción de la base de lectura:
1. si el `event_id` ya está en `processed_event`, es una entrega repetida;
2. si la vista ya tiene esa versión o una posterior, el evento está superado y
   solo se registra como procesado;
3. si es la siguiente versión (la 1 para un plan nuevo), crea la vista y registra
   el evento;
4. si falta una versión intermedia, falla sin efectos: el mensaje no se confirma
   y Pub/Sub lo reintenta hasta que llegue la anterior (o termina en la DLQ).
"""

import logging
from collections.abc import Callable
from enum import StrEnum

from training_management.application.messages import PlanCreatedEvent
from training_management.application.ports import Clock, DuplicateDelivery, ProjectionUnitOfWork
from training_management.domain.plans.events import EVENT_TYPE

_logger = logging.getLogger("sportapp.projector")


class ProjectionGap(Exception):
    """Llegó una versión posterior a la siguiente esperada; se reintenta más tarde."""


class ProjectionOutcome(StrEnum):
    PROJECTED = "projected"
    DUPLICATE = "duplicate"
    STALE = "stale"


class ProjectPlanCreated:
    def __init__(self, *, unit_of_work: Callable[[], ProjectionUnitOfWork], clock: Clock) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(self, event: PlanCreatedEvent) -> ProjectionOutcome:
        try:
            return self._project(event)
        except DuplicateDelivery:
            return ProjectionOutcome.DUPLICATE

    def _project(self, event: PlanCreatedEvent) -> ProjectionOutcome:
        now = self._clock.now()
        plan_id = event.plan.plan_id
        with self._unit_of_work() as unit_of_work:
            if unit_of_work.is_processed(event.event_id):
                return ProjectionOutcome.DUPLICATE
            projected = unit_of_work.projected_version(plan_id)
            if projected is not None and projected >= event.aggregate_version:
                outcome = ProjectionOutcome.STALE
            elif (projected or 0) + 1 == event.aggregate_version:
                unit_of_work.add_plan_view(event.plan, now)
                # HU035: las sesiones del plan, consultables por fecha (ADR-031).
                unit_of_work.save_planned_sessions(event.plan, now)
                outcome = ProjectionOutcome.PROJECTED
            else:
                _logger.warning(
                    "projector.version_gap",
                    extra={
                        "fields": {
                            "plan_id": str(plan_id),
                            "projected_version": projected,
                            "event_version": event.aggregate_version,
                        }
                    },
                )
                raise ProjectionGap()
            unit_of_work.add_processed(event.event_id, EVENT_TYPE, plan_id, now)
            unit_of_work.commit()
        return outcome
