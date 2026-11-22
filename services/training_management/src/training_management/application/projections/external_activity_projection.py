"""`entrenamientos-external-activity-consumer`: proyectar `actividad.importada.v1` (HU031).

En una sola transacción de la base de lectura (ADR-042 §12):
1. si el `event_id` ya está en `processed_event`, es una entrega repetida;
2. si la fila de `training_session_history` (`session_id = activity_id`) no existe, se inserta
   con `source = STRAVA`; si existe con una versión menor, se reemplaza (cada evento trae la
   actividad completa, así que no hace falta la versión inmediatamente siguiente); si tiene
   la misma versión o una mayor, el evento está superado y solo se registra como procesado;
3. se registra el `event_id` y se confirma; el mensaje se confirma (ACK) después del COMMIT.

Nunca crea una sesión `ACTIVE` ni publica eventos de HU032.
"""

from collections.abc import Callable

from training_management.application.external_activity import (
    EVENT_TYPE,
    ExternalActivityImported,
    ExternalActivityUnitOfWork,
)
from training_management.application.ports import Clock, DuplicateDelivery
from training_management.application.projections.plan_projection import ProjectionOutcome


class ProjectExternalActivity:
    def __init__(
        self, *, unit_of_work: Callable[[], ExternalActivityUnitOfWork], clock: Clock
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(self, event: ExternalActivityImported) -> ProjectionOutcome:
        try:
            return self._project(event)
        except DuplicateDelivery:
            return ProjectionOutcome.DUPLICATE

    def _project(self, event: ExternalActivityImported) -> ProjectionOutcome:
        now = self._clock.now()
        activity = event.activity
        with self._unit_of_work() as unit_of_work:
            if unit_of_work.is_processed(event.event_id):
                return ProjectionOutcome.DUPLICATE
            applied = unit_of_work.save_external_session(activity, now)
            unit_of_work.add_processed(event.event_id, EVENT_TYPE, activity.activity_id, now)
            unit_of_work.commit()
        return ProjectionOutcome.PROJECTED if applied else ProjectionOutcome.STALE
