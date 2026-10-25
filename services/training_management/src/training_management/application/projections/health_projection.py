"""HU042: proyectar `reporte_salud.registrado.v1` en `health_report_view` (ADR-037, CMP-003).

El evento trae el estado completo del reporte. En una sola transacción de la base de
lectura:
1. si el `event_id` ya está en `processed_event`, es una entrega repetida (sin efectos);
2. si el reporte no está proyectado o lo está con una versión menor, se inserta o reemplaza
   su estado junto con `processed_event`;
3. si ya está con una versión igual o mayor (p. ej., la reconstrucción desde la outbox),
   solo se registra el evento como procesado, sin retroceder la vista.
La proyección no diagnostica ni reevalúa nada: representa el reporte confirmado.
"""

from collections.abc import Callable

from training_management.application.messages import HealthReportRegisteredEvent
from training_management.application.ports import Clock, DuplicateDelivery, ProjectionUnitOfWork
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.domain.health.events import EVENT_TYPE


class ProjectHealthReportRegistered:
    def __init__(self, *, unit_of_work: Callable[[], ProjectionUnitOfWork], clock: Clock) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(self, event: HealthReportRegisteredEvent) -> ProjectionOutcome:
        report = event.report
        now = self._clock.now()
        try:
            with self._unit_of_work() as unit_of_work:
                if unit_of_work.is_processed(event.event_id):
                    return ProjectionOutcome.DUPLICATE
                projected = unit_of_work.health_report_version(report.report_id)
                if projected is not None and projected >= report.version:
                    outcome = ProjectionOutcome.STALE
                else:
                    unit_of_work.save_health_report_view(report, now)
                    outcome = ProjectionOutcome.PROJECTED
                unit_of_work.add_processed(event.event_id, EVENT_TYPE, report.report_id, now)
                unit_of_work.commit()
        except DuplicateDelivery:
            return ProjectionOutcome.DUPLICATE
        return outcome
