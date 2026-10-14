"""HU034: proyectar `indicador.calculado.v1` en `training_metric_result` (ADR-030, CMP-003).

El cálculo es inmutable (versión 1) y el evento trae su estado completo. En una sola
transacción de la base de lectura:
1. si el `event_id` ya está en `processed_event`, es una entrega repetida (sin efectos);
2. si el cálculo no está proyectado, se inserta junto con `processed_event`;
3. si ya está con los mismos datos (p. ej., la reconstrucción desde la outbox), solo se
   registra el evento como procesado;
4. si está con otros datos, es un conflicto: falla sin efectos, el mensaje no se confirma
   y, agotados los reintentos, termina en la DLQ.
No se valida contra el catálogo: cualquier método escalar compatible se proyecta igual.
"""

import logging
from collections.abc import Callable

from training_management.application.messages import MetricCalculatedEvent
from training_management.application.ports import Clock, DuplicateDelivery, ProjectionUnitOfWork
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.domain.metrics.events import EVENT_TYPE

_logger = logging.getLogger("sportapp.projector")


class MetricResultConflict(Exception):
    """El mismo `calculation_id` ya está proyectado con otros datos."""


class ProjectMetricCalculated:
    def __init__(self, *, unit_of_work: Callable[[], ProjectionUnitOfWork], clock: Clock) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(self, event: MetricCalculatedEvent) -> ProjectionOutcome:
        calculation = event.calculation
        now = self._clock.now()
        try:
            with self._unit_of_work() as unit_of_work:
                if unit_of_work.is_processed(event.event_id):
                    return ProjectionOutcome.DUPLICATE
                projected = unit_of_work.metric_result(calculation.calculation_id)
                if projected is None:
                    unit_of_work.add_metric_result(calculation, now)
                    outcome = ProjectionOutcome.PROJECTED
                elif projected == calculation:
                    outcome = ProjectionOutcome.STALE
                else:
                    # Solo identificadores: entradas y resultados son datos sensibles.
                    _logger.warning(
                        "projector.metric_conflict",
                        extra={"fields": {"calculation_id": str(calculation.calculation_id)}},
                    )
                    raise MetricResultConflict()
                unit_of_work.add_processed(
                    event.event_id, EVENT_TYPE, calculation.calculation_id, now
                )
                unit_of_work.commit()
        except DuplicateDelivery:
            return ProjectionOutcome.DUPLICATE
        return outcome
