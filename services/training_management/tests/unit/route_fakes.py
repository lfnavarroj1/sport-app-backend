"""Dobles en memoria para probar HU026 sin infraestructura (datos sintéticos)."""

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

from training_management.application.errors import IdempotencyKeyConflict
from training_management.application.ports import (
    CommandRecord,
    MyRoutesQuery,
    OutboxMessage,
    RouteSuggestionQuery,
)
from training_management.domain.plans.model import Sport
from training_management.domain.routes.model import (
    Difficulty,
    GeoPoint,
    Route,
    RouteInput,
    RouteStatus,
)
from training_management.domain.routes.suggestions import personalized_order

AUTHOR = "training.routes.author"
SUGGEST = "training.routes.suggest"


def route_body(**overrides: Any) -> dict[str, Any]:
    """Cuerpo válido de `POST /v1/routes` con datos sintéticos."""
    body: dict[str, Any] = {
        "name": "Vuelta al embalse",
        "description": "Recorrido llano por la orilla.",
        "sport": "running",
        "distance_km": "12.5",
        "difficulty": "MODERATE",
        "city": "Medellín",
        "country_code": "CO",
        "start_point": {"lat": 6.2442, "lng": -75.5812},
        "waypoints": [{"lat": 6.25, "lng": -75.57}, {"lat": 6.26, "lng": -75.56}],
    }
    body.update(overrides)
    return body


def route_input(**overrides: Any) -> RouteInput:
    values: dict[str, Any] = {
        "name": "Vuelta al embalse",
        "description": "Recorrido llano por la orilla.",
        "sport": Sport.RUNNING,
        "distance_km": Decimal("12.5"),
        "difficulty": Difficulty.MODERATE,
        "city": "Medellín",
        "country_code": "CO",
        "start_point": GeoPoint(lat=6.2442, lng=-75.5812),
        "waypoints": (GeoPoint(lat=6.25, lng=-75.57),),
    }
    values.update(overrides)
    return RouteInput(**values)


@dataclass
class InMemoryRouteStore:
    routes: dict[UUID, Route] = field(default_factory=dict)
    outbox: list[OutboxMessage] = field(default_factory=list)
    commands: dict[tuple[UUID, str, UUID], CommandRecord] = field(default_factory=dict)


class InMemoryRouteUnitOfWork:
    """Aplica los cambios solo al confirmar y valida la misma unicidad que PostgreSQL."""

    def __init__(
        self, store: InMemoryRouteStore, before_commit: Callable[[], None] | None = None
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self._routes: list[Route] = []
        self._outbox: list[OutboxMessage] = []
        self._commands: list[CommandRecord] = []
        self._deleted: list[tuple[UUID, str, UUID]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._routes, self._outbox, self._commands, self._deleted = [], [], [], []

    def find_command(self, actor_id: UUID, operation: str, key: UUID) -> CommandRecord | None:
        return self.store.commands.get((actor_id, operation, key))

    def delete_command(self, actor_id: UUID, operation: str, key: UUID) -> None:
        self._deleted.append((actor_id, operation, key))

    def add_command(self, record: CommandRecord) -> None:
        self._commands.append(record)

    def get_route_for_update(self, route_id: UUID) -> Route | None:
        return self.store.routes.get(route_id)

    def add_route(self, route: Route) -> None:
        self._routes.append(route)

    def update_route(self, route: Route) -> None:
        self._routes.append(route)

    def add_outbox(self, message: OutboxMessage) -> None:
        self._outbox.append(message)

    def commit(self) -> None:
        if self.before_commit is not None:
            hook, self.before_commit = self.before_commit, None
            hook()
        for key in self._deleted:
            self.store.commands.pop(key, None)
        for record in self._commands:
            if (record.actor_id, record.operation, record.key) in self.store.commands:
                raise IdempotencyKeyConflict()
        for route in self._routes:
            self.store.routes[route.route_id] = route
        self.store.outbox.extend(self._outbox)
        for record in self._commands:
            self.store.commands[(record.actor_id, record.operation, record.key)] = record


class InMemoryRouteReader:
    """`route_view` en memoria con el mismo orden que la consulta SQL."""

    def __init__(self, routes: dict[UUID, Route]) -> None:
        self.routes = routes
        self.suggest_queries: list[RouteSuggestionQuery] = []

    def list_by_organizer(self, query: MyRoutesQuery) -> list[Route]:
        own = sorted(
            (route for route in self.routes.values() if route.organizer_id == query.organizer_id),
            key=lambda route: (route.created_at, route.route_id),
            reverse=True,
        )
        if query.after is not None:
            after = (query.after.created_at, query.after.route_id)
            own = [route for route in own if (route.created_at, route.route_id) < after]
        return own[: query.limit]

    def suggest(self, query: RouteSuggestionQuery) -> list[Route]:
        self.suggest_queries.append(query)
        published = [
            route for route in self.routes.values() if route.status is RouteStatus.PUBLISHED
        ]
        context = query.context
        if not context.personalized:
            published.sort(key=lambda route: (route.published_at, route.route_id), reverse=True)
            return published[: query.limit]
        matching = [route for route in published if route.sport in context.sports]
        matching.sort(
            key=lambda route: personalized_order(
                context,
                city=route.city,
                country_code=route.country_code,
                distance_km=route.distance_km,
                route_id=route.route_id,
            )
        )
        return matching[: query.limit]


def new_ids(count: int = 50) -> Callable[[], UUID]:
    ids = iter([uuid4() for _ in range(count)])
    return lambda: next(ids)
