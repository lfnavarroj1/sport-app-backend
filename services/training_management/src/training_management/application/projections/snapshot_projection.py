"""Proyecciones de HU011 y HU032 a partir de eventos con el estado completo (snapshot).

`autorizacion_entrenamiento.otorgada.v1` / `.revocada.v1` y `entrenamiento.finalizado.v1`
traen el estado completo del agregado, así que no hace falta exigir la versión
siguiente: en una sola transacción de lectura se aplica el evento solo si su versión
es mayor que la proyectada; uno igual o anterior queda registrado como superado. Así
un duplicado, un evento atrasado o el backfill de autorizaciones nunca retroceden la
vista y no bloquean la secuencia.

HU032 (ADR-029): `entrenamiento.iniciado.v1` (versión 1) crea `active_training_session`
salvo que la sesión ya figure en el historial o con una versión igual o mayor;
`entrenamiento.finalizado.v1` (versión 2) la elimina y actualiza el historial en la misma
transacción, aunque llegue antes que el inicio.

HU034 (ADR-030): `indicador.calculado.v1` se despacha a `ProjectMetricCalculated`.

HU014 (ADR-037): `plan.alimentacion_creado.v1` se despacha a `ProjectNutritionPlanCreated`.
HU026 (ADR-041): `ruta.publicada.v1` y `ruta.retirada.v1` se despachan a `ProjectRouteChanged`.
HU028 (ADR-041): `plantilla_entrenamiento.*.v1` se despachan a `ProjectTemplateChanged`.
HU036 (ADR-044): `medicion.recibida.v1` y `riesgo.detectado.v1` se despachan a
`ProjectMeasurementReceived` y `ProjectRiskDetected`.
HU042 (ADR-037): `reporte_salud.registrado.v1` se despacha a `ProjectHealthReportRegistered`.
"""

from collections.abc import Callable

from training_management.application.messages import (
    AuthorizationChangedEvent,
    HealthReportRegisteredEvent,
    MeasurementReceivedEvent,
    MetricCalculatedEvent,
    NutritionPlanCreatedEvent,
    PlanCreatedEvent,
    RiskDetectedEvent,
    RouteChangedEvent,
    SessionFinishedEvent,
    SessionStartedEvent,
    TemplateChangedEvent,
    TrainingEvent,
)
from training_management.application.ports import Clock, DuplicateDelivery, ProjectionUnitOfWork
from training_management.application.projections.health_projection import (
    ProjectHealthReportRegistered,
)
from training_management.application.projections.metric_projection import (
    ProjectMetricCalculated,
)
from training_management.application.projections.monitoring_projection import (
    ProjectMeasurementReceived,
    ProjectRiskDetected,
)
from training_management.application.projections.nutrition_projection import (
    ProjectNutritionPlanCreated,
)
from training_management.application.projections.plan_projection import (
    ProjectionOutcome,
    ProjectPlanCreated,
)
from training_management.application.projections.route_projection import ProjectRouteChanged
from training_management.application.projections.template_projection import (
    ProjectTemplateChanged,
)
from training_management.domain.sessions.events import EVENT_TYPE as SESSION_FINISHED
from training_management.domain.sessions.events import STARTED_EVENT_TYPE as SESSION_STARTED


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
                # Al terminar desaparece la sesión activa (HU032 CA8), aunque el inicio
                # todavía no se haya proyectado.
                unit_of_work.delete_active_session(entry.session_id)
                unit_of_work.add_processed(event.event_id, SESSION_FINISHED, entry.session_id, now)
                unit_of_work.commit()
        except DuplicateDelivery:
            return ProjectionOutcome.DUPLICATE
        return outcome


class ProjectSessionStarted:
    def __init__(self, *, unit_of_work: Callable[[], ProjectionUnitOfWork], clock: Clock) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(self, event: SessionStartedEvent) -> ProjectionOutcome:
        view = event.session
        now = self._clock.now()
        try:
            with self._unit_of_work() as unit_of_work:
                if unit_of_work.is_processed(event.event_id):
                    return ProjectionOutcome.DUPLICATE
                finished = unit_of_work.session_version(view.session_id)
                active = unit_of_work.active_session_version(view.session_id)
                if finished is not None or (active is not None and active >= view.version):
                    outcome = ProjectionOutcome.STALE
                else:
                    unit_of_work.save_active_session(view, now)
                    outcome = ProjectionOutcome.PROJECTED
                unit_of_work.add_processed(event.event_id, SESSION_STARTED, view.session_id, now)
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
        self._started = ProjectSessionStarted(unit_of_work=unit_of_work, clock=clock)
        self._metrics = ProjectMetricCalculated(unit_of_work=unit_of_work, clock=clock)
        self._nutrition = ProjectNutritionPlanCreated(unit_of_work=unit_of_work, clock=clock)
        self._routes = ProjectRouteChanged(unit_of_work=unit_of_work, clock=clock)
        self._templates = ProjectTemplateChanged(unit_of_work=unit_of_work, clock=clock)
        self._measurements = ProjectMeasurementReceived(unit_of_work=unit_of_work, clock=clock)
        self._risks = ProjectRiskDetected(unit_of_work=unit_of_work, clock=clock)
        self._health_reports = ProjectHealthReportRegistered(unit_of_work=unit_of_work, clock=clock)

    def __call__(self, event: TrainingEvent) -> ProjectionOutcome:
        if isinstance(event, PlanCreatedEvent):
            return self._plans(event)
        if isinstance(event, AuthorizationChangedEvent):
            return self._authorizations(event)
        if isinstance(event, SessionStartedEvent):
            return self._started(event)
        if isinstance(event, MeasurementReceivedEvent):
            return self._measurements(event)
        if isinstance(event, RiskDetectedEvent):
            return self._risks(event)
        return self._project_snapshot(event)

    def _project_snapshot(self, event: TrainingEvent) -> ProjectionOutcome:
        if isinstance(event, MetricCalculatedEvent):
            return self._metrics(event)
        if isinstance(event, NutritionPlanCreatedEvent):
            return self._nutrition(event)
        if isinstance(event, RouteChangedEvent):
            return self._routes(event)
        if isinstance(event, TemplateChangedEvent):
            return self._templates(event)
        if isinstance(event, SessionFinishedEvent):
            return self._sessions(event)
        if isinstance(event, HealthReportRegisteredEvent):
            return self._health_reports(event)
        raise TypeError(type(event).__name__)
