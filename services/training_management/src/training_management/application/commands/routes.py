"""HU026: publicar y retirar rutas sugeribles (ADR-041).

Publicación (`POST /v1/routes`, CA1 y CA2):
1. Permiso por actor `training.routes.author` (solo `organizer`).
2. Validar la ruta con las reglas del agregado; si incumple alguna, `validation_error` con
   todos los detalles y nada se publica.
3. Clave de idempotencia por organizador y operación, vigente 24 horas: el reintento devuelve
   el resultado original; la misma clave con otra ruta es `idempotency_key_reused`.
4. En una transacción: ruta `PUBLISHED` versión 1, `ruta.publicada.v1` en la outbox y el
   resultado idempotente. Si una solicitud concurrente con la misma clave confirma primero,
   se devuelve su resultado.

Retiro (`POST /v1/routes/{route_id}/retire`, CA4), en una transacción con la fila bloqueada:
- una ruta inexistente o de otro organizador es `route_not_found`, sin revelar cuál;
- una ruta ya retirada se devuelve tal cual, sin otro evento (el retiro es idempotente);
- si `expected_version` no es la versión vigente, `version_conflict` con la versión vigente;
- si no, `RETIRED` con la versión siguiente y `ruta.retirada.v1` en la outbox.
La fila se conserva. Las respuestas se emiten después del COMMIT y no esperan la proyección.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from training_management.application.access import require_route_author
from training_management.application.errors import (
    IdempotencyKeyConflict,
    IdempotencyKeyReused,
    PermissionRequired,
)
from training_management.application.ports import (
    Caller,
    Clock,
    CommandRecord,
    OutboxMessage,
    PermissionPolicy,
    RouteUnitOfWork,
)
from training_management.application.route_documents import route_from_document
from training_management.domain.routes.events import (
    AGGREGATE_TYPE,
    PUBLISHED_EVENT_TYPE,
    RETIRED_EVENT_TYPE,
    SCHEMA_VERSION,
    route_document,
)
from training_management.domain.routes.model import (
    InvalidRoute,
    Route,
    RouteInput,
    RouteNotFound,
    RouteStatus,
    RouteVersionConflict,
)

PUBLISH_OPERATION = "publish_route"
IDEMPOTENCY_TTL = timedelta(hours=24)

_audit = logging.getLogger("sportapp.audit")


@dataclass(frozen=True)
class PublishRouteCommand:
    data: RouteInput
    idempotency_key: UUID
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class RetireRouteCommand:
    route_id: UUID
    expected_version: int
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class RouteResult:
    route: Route
    replayed: bool = False


def request_fingerprint(route: Route) -> str:
    """Identifica la solicitud con los datos normalizados (`12` y `12.0` son la misma)."""
    document = route_document(route)
    canonical = json.dumps(
        [
            PUBLISH_OPERATION,
            {
                key: document[key]
                for key in (
                    "organizer_id",
                    "name",
                    "description",
                    "sport",
                    "distance_km",
                    "difficulty",
                    "city",
                    "country_code",
                    "start_point",
                    "waypoints",
                )
            },
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _outbox(
    route: Route, event_type: str, event_id: UUID, correlation_id: UUID | None, causation: str
) -> OutboxMessage:
    return OutboxMessage(
        event_id=event_id,
        event_type=event_type,
        schema_version=SCHEMA_VERSION,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=route.route_id,
        aggregate_version=route.version,
        occurred_at=route.updated_at,
        correlation_id=correlation_id,
        causation_id=causation,
        payload=route_document(route),
    )


def _audit_result(name: str, caller: Caller, result: str, **fields: str) -> None:
    # Solo identificadores y el resultado: ni nombres ni coordenadas.
    _audit.info(
        name,
        extra={"fields": {"organizer_id": str(caller.user_id), "result": result, **fields}},
    )


class PublishRoute:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], RouteUnitOfWork],
        permissions: PermissionPolicy,
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._permissions = permissions
        self._clock = clock
        self._new_id = new_id

    def __call__(self, caller: Caller, command: PublishRouteCommand) -> RouteResult:
        try:
            result = self._execute(caller, command)
        except (PermissionRequired, InvalidRoute, IdempotencyKeyReused) as error:
            _audit_result("audit.route_published", caller, "rejected", reason=error.code)
            raise
        _audit_result(
            "audit.route_published",
            caller,
            "replayed" if result.replayed else "published",
            route_id=str(result.route.route_id),
        )
        return result

    def _execute(self, caller: Caller, command: PublishRouteCommand) -> RouteResult:
        require_route_author(caller, self._permissions)
        route = Route.publish(
            route_id=self._new_id(),
            organizer_id=caller.user_id,
            data=command.data,
            now=self._clock.now(),
        )
        fingerprint = request_fingerprint(route)
        previous = self._previous(caller.user_id, command.idempotency_key, fingerprint)
        if previous is not None:
            return previous
        try:
            return self._publish(route, command, fingerprint)
        except IdempotencyKeyConflict:
            previous = self._previous(caller.user_id, command.idempotency_key, fingerprint)
            if previous is None:
                raise
            return previous

    def _previous(self, organizer_id: UUID, key: UUID, fingerprint: str) -> RouteResult | None:
        with self._unit_of_work() as unit_of_work:
            record = unit_of_work.find_command(organizer_id, PUBLISH_OPERATION, key)
        if record is None or record.expires_at <= self._clock.now():
            return None
        if record.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused()
        return RouteResult(route=route_from_document(record.response), replayed=True)

    def _publish(self, route: Route, command: PublishRouteCommand, fingerprint: str) -> RouteResult:
        now = route.created_at
        with self._unit_of_work() as unit_of_work:
            existing = unit_of_work.find_command(
                route.organizer_id, PUBLISH_OPERATION, command.idempotency_key
            )
            if existing is not None:
                if existing.expires_at > now:
                    raise IdempotencyKeyConflict()
                unit_of_work.delete_command(
                    route.organizer_id, PUBLISH_OPERATION, command.idempotency_key
                )
            unit_of_work.add_route(route)
            unit_of_work.add_outbox(
                _outbox(
                    route,
                    PUBLISHED_EVENT_TYPE,
                    self._new_id(),
                    command.correlation_id,
                    str(command.idempotency_key),
                )
            )
            unit_of_work.add_command(
                CommandRecord(
                    actor_id=route.organizer_id,
                    operation=PUBLISH_OPERATION,
                    key=command.idempotency_key,
                    request_fingerprint=fingerprint,
                    resource_id=route.route_id,
                    response=route_document(route),
                    created_at=now,
                    expires_at=now + IDEMPOTENCY_TTL,
                )
            )
            unit_of_work.commit()
        return RouteResult(route=route)


class RetireRoute:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], RouteUnitOfWork],
        permissions: PermissionPolicy,
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._permissions = permissions
        self._clock = clock
        self._new_id = new_id

    def __call__(self, caller: Caller, command: RetireRouteCommand) -> RouteResult:
        fields = {"route_id": str(command.route_id)}
        try:
            result = self._execute(caller, command)
        except (PermissionRequired, RouteNotFound, RouteVersionConflict) as error:
            _audit_result("audit.route_retired", caller, "rejected", reason=error.code, **fields)
            raise
        _audit_result(
            "audit.route_retired", caller, "replayed" if result.replayed else "retired", **fields
        )
        return result

    def _execute(self, caller: Caller, command: RetireRouteCommand) -> RouteResult:
        require_route_author(caller, self._permissions)
        with self._unit_of_work() as unit_of_work:
            route = unit_of_work.get_route_for_update(command.route_id)
            if route is None or route.organizer_id != caller.user_id:
                raise RouteNotFound()
            if route.status is RouteStatus.RETIRED:
                return RouteResult(route=route, replayed=True)
            if route.version != command.expected_version:
                raise RouteVersionConflict(route.version)
            retired = route.retire(self._clock.now())
            unit_of_work.update_route(retired)
            unit_of_work.add_outbox(
                _outbox(
                    retired,
                    RETIRED_EVENT_TYPE,
                    self._new_id(),
                    command.correlation_id,
                    str(retired.route_id),
                )
            )
            unit_of_work.commit()
        return RouteResult(route=retired)
