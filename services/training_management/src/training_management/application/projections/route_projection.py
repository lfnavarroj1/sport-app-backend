"""HU026: proyectar `ruta.publicada.v1` y `ruta.retirada.v1` en `route_view` (ADR-041, CMP-003).

Ambos eventos traen el estado completo de la ruta. En una sola transacción de la base de
lectura:
1. si el `event_id` ya está en `processed_event`, es una entrega repetida (sin efectos);
2. si la ruta no está proyectada, se inserta; si la versión del evento es mayor que la
   proyectada, se reemplaza el estado completo (un retiro que llega antes que su publicación
   deja la ruta `RETIRED` y la publicación posterior queda superada);
3. si la vista ya tiene esa versión o una posterior, el evento está superado y solo se
   registra como procesado.
Un evento que no cumple el contrato no llega aquí: `application.messages` lo rechaza y,
agotados los reintentos, termina en la DLQ.
"""

from collections.abc import Callable

from training_management.application.messages import RouteChangedEvent
from training_management.application.ports import Clock, DuplicateDelivery, ProjectionUnitOfWork
from training_management.application.projections.plan_projection import ProjectionOutcome


class ProjectRouteChanged:
    def __init__(self, *, unit_of_work: Callable[[], ProjectionUnitOfWork], clock: Clock) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(self, event: RouteChangedEvent) -> ProjectionOutcome:
        route = event.route
        now = self._clock.now()
        try:
            with self._unit_of_work() as unit_of_work:
                if unit_of_work.is_processed(event.event_id):
                    return ProjectionOutcome.DUPLICATE
                projected = unit_of_work.route_version(route.route_id)
                if projected is not None and projected >= route.version:
                    outcome = ProjectionOutcome.STALE
                else:
                    unit_of_work.save_route_view(route, now)
                    outcome = ProjectionOutcome.PROJECTED
                unit_of_work.add_processed(event.event_id, event.event_type, route.route_id, now)
                unit_of_work.commit()
        except DuplicateDelivery:
            return ProjectionOutcome.DUPLICATE
        return outcome
