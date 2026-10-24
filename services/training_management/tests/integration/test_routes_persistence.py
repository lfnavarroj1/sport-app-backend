"""HU026 (ADR-041) contra PostgreSQL real (requiere `TEST_DATABASE_URL`): ruta, outbox y clave
en una transacción; reintentos y carreras con la misma clave; retiro con la fila bloqueada;
outbox → proyector → `route_view`; orden SQL igual al del dominio; retirada no sugerida; ruta
ajena `404`; 500 rutas con p95 < 2 s (HA010) y migraciones `0007_routes` / `0009_route_view`."""

import random
import statistics
import threading
import time
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, insert, inspect, pool, select
from support import READ_HISTORY, WRITE_HISTORY, TrainingDatabases, training_databases

from sportapp_platform.migrations import current_revision, downgrade, upgrade
from sportapp_platform.testing.postgres import temporary_database
from training_management.application.commands.routes import (
    PublishRoute,
    PublishRouteCommand,
    RetireRoute,
    RetireRouteCommand,
)
from training_management.application.messages import parse_training_event
from training_management.application.ports import Caller, RouteSuggestionQuery
from training_management.application.queries.routes import (
    ListMyRoutes,
    MyRoutesFilters,
    SuggestionFilters,
    SuggestRoutes,
)
from training_management.domain.plans.model import Sport
from training_management.domain.routes.model import (
    Difficulty,
    GeoPoint,
    Route,
    RouteInput,
    RouteNotFound,
    RouteStatus,
    RouteVersionConflict,
)
from training_management.domain.routes.suggestions import SuggestionContext, personalized_order
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.persistence_read.models import RouteViewRow
from training_management.infrastructure.persistence_read.routes import (
    SqlAlchemyRouteReader,
    route_view_values,
)
from training_management.infrastructure.persistence_write.models import (
    CommandIdempotencyRow,
    OutboxEventRow,
    RouteRow,
)
from training_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from training_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyRouteUnitOfWork,
)
from training_management.workers.outbox_publisher import build_envelope

CONCURRENCY = 8
P95_LIMIT_SECONDS = 2.0
CITIES = ("Medellín", "Bogotá", "Cali", "Barranquilla", "Pereira")


class RouteAuthor:
    def allows(self, actor_type: str, permission: str) -> bool:
        return actor_type == "organizer" and permission == "training.routes.author"


class SuggestCapable:
    def has_capability(self, user_id: UUID, capability: str) -> bool:
        return capability == "training.routes.suggest"


def route_input(**overrides: Any) -> RouteInput:
    values: dict[str, Any] = {
        "name": "Vuelta al embalse",
        "description": None,
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


def run_concurrently(task: Callable[[], Any], times: int = CONCURRENCY) -> list[Any]:
    barrier = threading.Barrier(times)
    results: list[Any] = [None] * times

    def worker(index: int) -> None:
        barrier.wait()
        try:
            results[index] = task()
        except Exception as error:
            results[index] = error

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(times)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


class RoutePersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: TrainingDatabases = self.enterContext(training_databases("hu026"))
        self.organizer = Caller(uuid4(), "organizer")
        self.publish = PublishRoute(
            unit_of_work=lambda: SqlAlchemyRouteUnitOfWork(self.db.writes),
            permissions=RouteAuthor(),
            clock=SystemClock(),
        )
        self.retire = RetireRoute(
            unit_of_work=lambda: SqlAlchemyRouteUnitOfWork(self.db.writes),
            permissions=RouteAuthor(),
            clock=SystemClock(),
        )
        self.reader = SqlAlchemyRouteReader(self.db.reads)

    def counts(self) -> tuple[int, int, int]:
        return (
            self.db.count(RouteRow),
            self.db.count(OutboxEventRow),
            self.db.count(CommandIdempotencyRow),
        )

    def project_outbox(self) -> None:
        store, project = SqlAlchemyOutboxStore(self.db.writes), self.db.project()
        for event in store.claim_batch("w-test", 100, 30):
            project(parse_training_event(build_envelope(event)))
            store.mark_published(event.event_id, "w-test")

    def test_route_outbox_and_key_commit_together_and_replay(self) -> None:
        """CA1: `PUBLISHED` v1 con autor, outbox y clave; el reintento no duplica."""
        key = uuid4()
        first = self.publish(
            self.organizer, PublishRouteCommand(route_input(distance_km=Decimal("12")), key)
        )
        again = self.publish(
            self.organizer, PublishRouteCommand(route_input(distance_km=Decimal("12.0")), key)
        )

        self.assertTrue(again.replayed)
        self.assertEqual(again.route, first.route)
        self.assertEqual(self.counts(), (1, 1, 1))
        with self.db.writes() as session:
            row = session.get(RouteRow, first.route.route_id)
            assert row is not None
            self.assertEqual(
                (row.status, row.version, row.organizer_id, row.distance_km),
                ("PUBLISHED", 1, self.organizer.user_id, Decimal("12.0")),
            )
            [event] = session.scalars(select(OutboxEventRow))
            self.assertEqual(
                (event.event_type, event.aggregate_type), ("ruta.publicada.v1", "route")
            )

    def test_concurrent_requests_with_the_same_key_publish_once(self) -> None:
        key = uuid4()
        results = run_concurrently(
            lambda: self.publish(self.organizer, PublishRouteCommand(route_input(), key))
        )

        self.assertTrue(all(not isinstance(result, Exception) for result in results), results)
        self.assertEqual({result.route.route_id for result in results}, {results[0].route.route_id})
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_concurrent_retires_emit_one_event(self) -> None:
        """CA4: con la fila bloqueada, dos retiros de la misma versión dan un solo evento."""
        route = self.publish(self.organizer, PublishRouteCommand(route_input(), uuid4())).route

        results = run_concurrently(
            lambda: self.retire(self.organizer, RetireRouteCommand(route.route_id, 1)), times=4
        )

        self.assertTrue(all(not isinstance(result, Exception) for result in results), results)
        self.assertEqual({result.route.version for result in results}, {2})
        self.assertEqual(self.counts()[1], 2)

    def test_foreign_route_is_not_found_and_stale_version_conflicts(self) -> None:
        """CA4: una ruta ajena no puede retirarse (`404`) y no cambia."""
        route = self.publish(self.organizer, PublishRouteCommand(route_input(), uuid4())).route

        with self.assertRaises(RouteNotFound):
            self.retire(Caller(uuid4(), "organizer"), RetireRouteCommand(route.route_id, 1))
        with self.assertRaises(RouteVersionConflict):
            self.retire(self.organizer, RetireRouteCommand(route.route_id, 2))
        with self.db.writes() as session:
            row = session.get(RouteRow, route.route_id)
            assert row is not None
            self.assertEqual((row.status, row.version), ("PUBLISHED", 1))

    def test_projection_suggests_and_retired_route_is_not_suggested(self) -> None:
        """CA3 y CA4: outbox → proyector → `route_view`; la retirada conserva su fila."""
        near = self.publish(
            self.organizer,
            PublishRouteCommand(route_input(name="Cerca", distance_km=Decimal("5.0")), uuid4()),
        ).route
        far = self.publish(
            self.organizer,
            PublishRouteCommand(
                route_input(name="Otra ciudad", city="Bogotá", distance_km=Decimal("2.0")),
                uuid4(),
            ),
        ).route
        retired = self.publish(
            self.organizer,
            PublishRouteCommand(route_input(name="Retirada", distance_km=Decimal("1.0")), uuid4()),
        ).route
        self.retire(self.organizer, RetireRouteCommand(retired.route_id, 1))
        self.project_outbox()
        suggest = SuggestRoutes(reader=self.reader, capabilities=SuggestCapable())

        result = suggest(
            Caller(uuid4(), "athlete"),
            SuggestionFilters(sports=(Sport.RUNNING,), city="MEDELLIN", country_code="CO"),
        )

        self.assertTrue(result.personalized)
        self.assertEqual(
            [
                (item.route.route_id, [reason.value for reason in item.reasons])
                for item in result.items
            ],
            [
                (near.route_id, ["SPORT_MATCH", "CITY_MATCH"]),
                (far.route_id, ["SPORT_MATCH"]),
            ],
        )
        with self.db.reads() as session:
            row = session.get(RouteViewRow, retired.route_id)
            assert row is not None
            self.assertEqual((row.status, row.version), ("RETIRED", 2))
        mine = ListMyRoutes(reader=self.reader, permissions=RouteAuthor())(
            self.organizer, MyRoutesFilters(limit=10)
        )
        self.assertEqual(
            [route.route_id for route in mine.items],
            [retired.route_id, far.route_id, near.route_id],
        )
        self.assertEqual(mine.items[0].status, RouteStatus.RETIRED)


class RouteSuggestionVolumeTest(unittest.TestCase):
    """HA010: 500 rutas en `route_view`; orden SQL = orden del dominio; p95 < 2 s."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._context = training_databases("hu026_volume")
        cls.db = cls._context.__enter__()
        generator = random.Random(26)  # noqa: S311  # nosec B311 - datos sintéticos
        base = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        cls.routes: list[Route] = []
        for index in range(500):
            route = Route.publish(
                route_id=uuid4(),
                organizer_id=uuid4(),
                data=route_input(
                    name=f"Ruta {index}",
                    sport=generator.choice(list(Sport)),
                    city=generator.choice(CITIES),
                    distance_km=Decimal(generator.randint(1, 5000)) / 10,
                ),
                now=base + timedelta(minutes=index),
            )
            if index % 10 == 0:
                route = route.retire(base + timedelta(days=1, minutes=index))
            cls.routes.append(route)
        with cls.db.reads() as session, session.begin():
            projected_at = datetime.now(UTC)
            session.execute(
                insert(RouteViewRow),
                [route_view_values(route, projected_at) for route in cls.routes],
            )
        cls.reader = SqlAlchemyRouteReader(cls.db.reads)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._context.__exit__(None, None, None)

    def expected(self, context: SuggestionContext, limit: int) -> list[UUID]:
        published = [route for route in self.routes if route.status is RouteStatus.PUBLISHED]
        if not context.personalized:
            published.sort(key=lambda route: (route.published_at, route.route_id), reverse=True)
            return [route.route_id for route in published[:limit]]
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
        return [route.route_id for route in matching[:limit]]

    def test_sql_order_matches_the_domain_order(self) -> None:
        for context, limit in (
            (SuggestionContext(sports=(Sport.RUNNING, Sport.CYCLING), city="cali"), 50),
            (SuggestionContext(sports=(Sport.SWIMMING,), city="Bogota", country_code="CO"), 50),
            (SuggestionContext(sports=(Sport.STRENGTH,)), 30),
            (SuggestionContext(sports=()), 20),
        ):
            with self.subTest(context=context):
                found = self.reader.suggest(RouteSuggestionQuery(context=context, limit=limit))

                self.assertEqual([route.route_id for route in found], self.expected(context, limit))
                self.assertTrue(all(route.status is RouteStatus.PUBLISHED for route in found))

    def test_p95_below_two_seconds_with_500_routes(self) -> None:
        suggest = SuggestRoutes(reader=self.reader, capabilities=SuggestCapable())
        athlete = Caller(uuid4(), "athlete")
        filters = (
            SuggestionFilters(sports=(Sport.RUNNING, Sport.CYCLING), city="Medellín", limit=50),
            SuggestionFilters(limit=50),
        )
        timings: list[float] = []
        for index in range(60):
            started = time.perf_counter()
            suggest(athlete, filters[index % 2])
            timings.append(time.perf_counter() - started)
        p95 = statistics.quantiles(timings, n=20)[-1]

        self.assertLess(p95, P95_LIMIT_SECONDS, f"p95 = {p95 * 1000:.1f} ms")


class RouteMigrationTest(unittest.TestCase):
    def test_write_and_read_migrations_upgrade_and_revert(self) -> None:
        for history, head, previous, table, indexes in (
            (
                WRITE_HISTORY,
                "0007_routes",
                "0006_nutrition_plans",
                "route",
                {"ix_route_organizer_id"},
            ),
            (
                READ_HISTORY,
                "0009_route_view",
                "0008_nutrition_plan_view",
                "route_view",
                {
                    "ix_route_view_status_sport_city",
                    "ix_route_view_organizer_order",
                    "ix_route_view_status_published",
                },
            ),
        ):
            with self.subTest(table=table), temporary_database(f"hu026_{table}") as url:
                upgrade(history, url, head)
                self.assertEqual(current_revision(url), head)
                self.assertTrue(indexes <= _indexes(url, table))

                downgrade(history, url, previous)

                self.assertNotIn(table, _tables(url))
                upgrade(history, url, head)
                self.assertEqual(current_revision(url), head)


def _indexes(url: str, table: str) -> set[str]:
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        return {str(index["name"]) for index in inspect(engine).get_indexes(table)}
    finally:
        engine.dispose()


def _tables(url: str) -> set[str]:
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
