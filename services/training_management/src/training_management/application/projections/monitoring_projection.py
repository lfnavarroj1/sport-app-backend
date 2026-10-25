"""HU036: proyectar `medicion.recibida.v1` y `riesgo.detectado.v1` (ADR-044, CMP-003).

`medicion.recibida.v1` trae el resumen de un lote, no el estado completo: la vista
`session_monitoring_view` acumula los resúmenes de la sesión y por eso aplica solo la versión
siguiente. En una sola transacción de la base de lectura:
1. si el `event_id` ya está en `processed_event`, es una entrega repetida (sin efectos);
2. una versión igual o anterior a la proyectada está superada: solo se registra;
3. la versión siguiente actualiza el acumulado (conteo, máximo, promedio y la muestra más
   reciente por `measured_at`, aunque los lotes lleguen con muestras fuera de orden);
4. una versión posterior a la siguiente es un hueco: falla sin efectos, el mensaje no se
   confirma y Pub/Sub lo reintenta (tras 10 intentos, DLQ). Solo se detiene esa sesión.

`riesgo.detectado.v1` es de inserción única por `alert_id` en `session_risk_alert_view`. La
proyección no reevalúa la regla ni diagnostica: representa alertas ya confirmadas.
"""

from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal

from training_management.application.messages import (
    MeasurementReceivedEvent,
    RiskDetectedEvent,
)
from training_management.application.ports import (
    Clock,
    DuplicateDelivery,
    MonitoringView,
    ProjectionUnitOfWork,
)
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.domain.monitoring.events import MEASUREMENT_EVENT_TYPE, RISK_EVENT_TYPE
from training_management.domain.monitoring.model import AVERAGE_QUANTUM, BatchSummary


class MonitoringVersionGap(Exception):
    """Falta una versión anterior del resumen de la sesión."""


def accumulate(current: MonitoringView | None, summary: BatchSummary) -> MonitoringView:
    """Suma el resumen del lote al acumulado de la sesión (versión siguiente)."""
    batch_sum = summary.avg_value * summary.samples_accepted
    if current is None:
        return MonitoringView(
            session_id=summary.session_id,
            device_id=summary.device_id,
            last_measured_at=summary.window_to,
            last_value=summary.last_value,
            samples_count=summary.samples_accepted,
            max_value=summary.max_value,
            avg_value=summary.avg_value,
            value_sum=batch_sum,
            version=summary.version,
        )
    count = current.samples_count + summary.samples_accepted
    value_sum = current.value_sum + batch_sum
    newer = summary.window_to > current.last_measured_at
    return MonitoringView(
        session_id=current.session_id,
        device_id=summary.device_id if newer else current.device_id,
        last_measured_at=summary.window_to if newer else current.last_measured_at,
        last_value=summary.last_value if newer else current.last_value,
        samples_count=count,
        max_value=max(current.max_value, summary.max_value),
        avg_value=(value_sum / Decimal(count)).quantize(AVERAGE_QUANTUM, ROUND_HALF_UP),
        value_sum=value_sum,
        version=summary.version,
    )


class ProjectMeasurementReceived:
    def __init__(self, *, unit_of_work: Callable[[], ProjectionUnitOfWork], clock: Clock) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(self, event: MeasurementReceivedEvent) -> ProjectionOutcome:
        summary = event.summary
        now = self._clock.now()
        try:
            with self._unit_of_work() as unit_of_work:
                if unit_of_work.is_processed(event.event_id):
                    return ProjectionOutcome.DUPLICATE
                current = unit_of_work.monitoring_view(summary.session_id)
                projected = current.version if current is not None else 0
                if summary.version <= projected:
                    outcome = ProjectionOutcome.STALE
                elif summary.version == projected + 1:
                    unit_of_work.save_monitoring_view(accumulate(current, summary), now)
                    outcome = ProjectionOutcome.PROJECTED
                else:
                    raise MonitoringVersionGap()
                unit_of_work.add_processed(
                    event.event_id, MEASUREMENT_EVENT_TYPE, summary.session_id, now
                )
                unit_of_work.commit()
        except DuplicateDelivery:
            return ProjectionOutcome.DUPLICATE
        return outcome


class ProjectRiskDetected:
    def __init__(self, *, unit_of_work: Callable[[], ProjectionUnitOfWork], clock: Clock) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(self, event: RiskDetectedEvent) -> ProjectionOutcome:
        alert = event.alert
        now = self._clock.now()
        try:
            with self._unit_of_work() as unit_of_work:
                if unit_of_work.is_processed(event.event_id):
                    return ProjectionOutcome.DUPLICATE
                if unit_of_work.has_risk_alert(alert.alert_id):
                    outcome = ProjectionOutcome.STALE
                else:
                    unit_of_work.add_risk_alert(alert, now)
                    outcome = ProjectionOutcome.PROJECTED
                unit_of_work.add_processed(event.event_id, RISK_EVENT_TYPE, alert.session_id, now)
                unit_of_work.commit()
        except DuplicateDelivery:
            return ProjectionOutcome.DUPLICATE
        return outcome
