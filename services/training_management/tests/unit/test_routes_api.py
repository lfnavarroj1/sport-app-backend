"""HU026 (ADR-041): API del servicio (autenticación, autorización, validación, idempotencia,
retiro, consulta propia y sugerencias) con routers reales, casos de uso con dobles en memoria y
tokens EdDSA firmados en la prueba."""

import unittest
import uuid
from datetime import timedelta
from typing import Any

from fakes import FakeCapabilities, FakePermissions, FixedClock
from fastapi.testclient import TestClient
from route_fakes import (
    AUTHOR,
    SUGGEST,
    InMemoryRouteReader,
    InMemoryRouteStore,
    InMemoryRouteUnitOfWork,
    route_body,
)
from test_api import VERIFIER, token

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from training_management.api.routes import build_routes_router
from training_management.application.commands.routes import PublishRoute, RetireRoute
from training_management.application.errors import CapabilitiesTimeout
from training_management.application.queries.routes import ListMyRoutes, SuggestRoutes
from training_management.domain.routes.events import PUBLISHED_EVENT_TYPE, RETIRED_EVENT_TYPE

ROUTES = "/v1/routes"
SUGGESTIONS = "/v1/me/suggestions/routes"


class _SlowCapabilities(FakeCapabilities):
    def has_capability(self, user_id: uuid.UUID, capability: str) -> bool:
        raise CapabilitiesTimeout()


class RoutesApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryRouteStore()
        self.clock = FixedClock()
        self.organizer_id, self.athlete_id = uuid.uuid4(), uuid.uuid4()
        self.capabilities = FakeCapabilities({self.athlete_id: {SUGGEST}})
        self.reader = InMemoryRouteReader(self.store.routes)
        self.client = self.client_with(self.capabilities)
        self.organizer = _bearer(self.organizer_id, "organizer")
        self.athlete = _bearer(self.athlete_id, "athlete")

    def client_with(self, capabilities: FakeCapabilities) -> TestClient:
        permissions = FakePermissions({"organizer": {AUTHOR}})
        app = create_service_app(
            service_name="training-management",
            title="test",
            api_version="test",
            settings=ServiceSettings(app_env="test"),  # type: ignore[call-arg]
            routers=[
                build_routes_router(
                    verify_token=VERIFIER.verify,
                    publish=PublishRoute(
                        unit_of_work=lambda: InMemoryRouteUnitOfWork(self.store),
                        permissions=permissions,
                        clock=self.clock,
                    ),
                    retire=RetireRoute(
                        unit_of_work=lambda: InMemoryRouteUnitOfWork(self.store),
                        permissions=permissions,
                        clock=self.clock,
                    ),
                    list_routes=ListMyRoutes(reader=self.reader, permissions=permissions),
                    suggest=SuggestRoutes(reader=self.reader, capabilities=capabilities),
                )
            ],
        )
        return TestClient(app)

    def publish(
        self, body: dict[str, Any] | None = None, key: str | None = None, headers: Any = None
    ) -> Any:
        return self.client.post(
            ROUTES,
            json=body or route_body(),
            headers={**(headers or self.organizer), "Idempotency-Key": key or str(uuid.uuid4())},
        )

    def retire(self, route_id: str, version: int = 1, headers: Any = None) -> Any:
        return self.client.post(
            f"{ROUTES}/{route_id}/retire",
            json={"expected_version": version},
            headers=headers or self.organizer,
        )

    # --- Publicación (CA1, CA2) ---

    def test_publishes_the_route_with_location_author_and_version(self) -> None:
        """CA1: `201`, `PUBLISHED`, versión 1, autor del token y distancia normalizada."""
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            response = self.publish(route_body(distance_km="12"))

        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(response.headers["Location"], f"{ROUTES}/{body['route_id']}")
        self.assertNotIn("Idempotent-Replayed", response.headers)
        self.assertEqual(
            (body["status"], body["version"], body["organizer_id"], body["distance_km"]),
            ("PUBLISHED", 1, str(self.organizer_id), "12.0"),
        )
        self.assertEqual(
            (body["sport"], body["difficulty"], body["city"], body["source"]),
            ("running", "MODERATE", "Medellín", "ORGANIZER"),
        )
        self.assertEqual(body["start_point"], {"lat": 6.2442, "lng": -75.5812})
        self.assertEqual(len(body["waypoints"]), 2)
        self.assertEqual(body["published_at"], "2026-10-12T12:00:00Z")
        self.assertIsNone(body["retired_at"])
        [event] = self.store.outbox
        self.assertEqual((event.event_type, event.aggregate_type), (PUBLISHED_EVENT_TYPE, "route"))
        self.assertEqual(str(event.aggregate_id), body["route_id"])
        self.assertEqual(event.payload, body)
        rendered = " ".join(audit.output)
        self.assertIn("audit.route_published", rendered)
        self.assertNotIn("Vuelta al embalse", rendered)

    def test_same_key_replays_without_publishing_again(self) -> None:
        """CA1: idempotente; la misma clave con otra ruta es `409`."""
        key = str(uuid.uuid4())
        first = self.publish(route_body(distance_km="12"), key=key)
        again = self.publish(route_body(distance_km="12.0"), key=key)
        other = self.publish(route_body(name="Otra ruta"), key=key)

        self.assertEqual(again.status_code, 201)
        self.assertEqual(again.headers["Idempotent-Replayed"], "true")
        self.assertEqual(again.json(), first.json())
        self.assertEqual(len(self.store.routes), 1)
        self.assertEqual(len(self.store.outbox), 1)
        self.assertEqual((other.status_code, other.json()["code"]), (409, "idempotency_key_reused"))

    def test_key_of_another_organizer_publishes_its_own_route(self) -> None:
        key = str(uuid.uuid4())
        self.publish(key=key)
        other = self.publish(key=key, headers=_bearer(uuid.uuid4(), "organizer"))

        self.assertEqual(other.status_code, 201)
        self.assertNotIn("Idempotent-Replayed", other.headers)
        self.assertEqual(len(self.store.routes), 2)

    def test_incomplete_or_out_of_range_routes_are_422_and_nothing_is_published(self) -> None:
        """CA2: `validation_error` con `details` por cada campo; nada se publica."""
        incomplete = route_body()
        del incomplete["start_point"], incomplete["name"]
        cases: dict[str, tuple[dict[str, Any], set[tuple[str, str]]]] = {
            "incompleta": (incomplete, {("body.start_point", "missing"), ("body.name", "missing")}),
            "distancia 0.0": (
                route_body(distance_km="0.0"),
                {("body.distance_km", "out_of_range")},
            ),
            "distancia 500.1": (
                route_body(distance_km="500.1"),
                {("body.distance_km", "out_of_range")},
            ),
            "dos decimales": (
                route_body(distance_km="1.25"),
                {("body.distance_km", "out_of_range")},
            ),
            "distancia no numérica": (
                route_body(distance_km="12 km"),
                {("body.distance_km", "string_pattern_mismatch")},
            ),
            "latitud": (
                route_body(start_point={"lat": 91, "lng": 0}),
                {("body.start_point.lat", "less_than_equal")},
            ),
            "longitud de un punto": (
                route_body(waypoints=[{"lat": 0, "lng": -181}]),
                {("body.waypoints.0.lng", "greater_than_equal")},
            ),
            "coordenada como texto": (
                route_body(start_point={"lat": "6.2", "lng": 0}),
                {("body.start_point.lat", "float_type")},
            ),
            "país": (route_body(country_code="XX"), {("body.country_code", "unknown_value")}),
            "deporte": (route_body(sport="hiking"), {("body.sport", "enum")}),
            "demasiados puntos": (
                route_body(waypoints=[{"lat": 0, "lng": 0}] * 201),
                {("body.waypoints", "too_long")},
            ),
            "campo de más": (route_body(gpx="..."), {("body.gpx", "extra_forbidden")}),
        }
        for name, (body, expected) in cases.items():
            with self.subTest(name=name):
                response = self.publish(body)

                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["code"], "validation_error")
                self.assertTrue(
                    expected
                    <= {(item["field"], item["issue"]) for item in response.json()["details"]},
                    response.json()["details"],
                )
        self.assertEqual((self.store.routes, self.store.outbox), ({}, []))

    def test_only_organizers_with_the_permission_publish(self) -> None:
        for actor in ("athlete", "service_provider"):
            with self.subTest(actor=actor):
                response = self.publish(headers=_bearer(uuid.uuid4(), actor))

                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )
        missing = self.client.post(ROUTES, json=route_body(), headers={"Idempotency-Key": "x"})
        self.assertEqual(missing.status_code, 422)
        unauthenticated = self.client.post(
            ROUTES, json=route_body(), headers={"Idempotency-Key": str(uuid.uuid4())}
        )
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(self.store.routes, {})

    # --- Retiro (CA4) ---

    def test_retire_keeps_the_route_as_retired_and_is_idempotent(self) -> None:
        """CA4: `RETIRED` versión 2 y un evento; repetir no publica otro."""
        route_id = self.publish().json()["route_id"]
        self.clock.advance(timedelta(hours=1))

        retired = self.retire(route_id)
        again = self.retire(route_id)

        self.assertEqual(retired.status_code, 200, retired.text)
        self.assertEqual((retired.json()["status"], retired.json()["version"]), ("RETIRED", 2))
        self.assertEqual(retired.json()["retired_at"], "2026-10-12T13:00:00Z")
        self.assertEqual(again.json(), retired.json())
        self.assertEqual(
            [event.event_type for event in self.store.outbox],
            [PUBLISHED_EVENT_TYPE, RETIRED_EVENT_TYPE],
        )
        self.assertEqual(self.store.outbox[1].aggregate_version, 2)
        self.assertIn(uuid.UUID(route_id), self.store.routes)

    def test_foreign_or_missing_route_is_404_and_stale_version_is_409(self) -> None:
        """CA4: una ruta ajena no puede retirarse; una versión vieja no se sobrescribe."""
        route_id = self.publish().json()["route_id"]

        foreign = self.retire(route_id, headers=_bearer(uuid.uuid4(), "organizer"))
        missing = self.retire(str(uuid.uuid4()))
        stale = self.retire(route_id, version=3)

        for response in (foreign, missing):
            self.assertEqual(
                (response.status_code, response.json()["code"]), (404, "route_not_found")
            )
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "version_conflict"))
        self.assertEqual(stale.json()["details"], [{"field": "expected_version", "issue": "1"}])
        self.assertEqual(len(self.store.outbox), 1)
        athlete = self.retire(route_id, headers=self.athlete)
        self.assertEqual(athlete.status_code, 403)
        invalid = self.client.post(
            f"{ROUTES}/{route_id}/retire", json={"expected_version": "1"}, headers=self.organizer
        )
        self.assertEqual(invalid.status_code, 422)

    # --- Mis rutas ---

    def test_my_routes_pages_without_losing_or_repeating(self) -> None:
        ids = []
        for minute in range(5):
            self.clock.advance(timedelta(minutes=minute))
            ids.append(self.publish().json()["route_id"])
        self.publish(headers=_bearer(uuid.uuid4(), "organizer"))

        seen, cursor = [], None
        while True:
            params = {"limit": "2", **({"cursor": cursor} if cursor else {})}
            response = self.client.get("/v1/me/routes", params=params, headers=self.organizer)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            seen.extend(item["route_id"] for item in response.json()["items"])
            cursor = response.json()["next_cursor"]
            if cursor is None:
                break

        self.assertEqual(seen, list(reversed(ids)))
        for params, field in (({"limit": "0"}, "query.limit"), ({"cursor": "x"}, "query.cursor")):
            with self.subTest(params=params):
                invalid = self.client.get("/v1/me/routes", params=params, headers=self.organizer)
                self.assertEqual(invalid.status_code, 422)
                self.assertEqual(invalid.json()["details"][0]["field"], field)
        forbidden = self.client.get("/v1/me/routes", headers=self.athlete)
        self.assertEqual(forbidden.status_code, 403)

    # --- Sugerencias (CA3, CA4) ---

    def seed(self) -> dict[str, str]:
        names = {}
        for name, sport, city, distance in (
            ("Bogotá corta", "running", "Bogotá", "3.0"),
            ("Medellín larga", "running", "Medellín", "21.1"),
            ("Medellín corta", "running", "MEDELLIN", "5.0"),
            ("Ciclovía", "cycling", "Medellín", "40.0"),
            ("Natación", "swimming", "Medellín", "1.5"),
        ):
            self.clock.advance(timedelta(minutes=1))
            body = route_body(name=name, sport=sport, city=city, distance_km=distance)
            names[name] = self.publish(body).json()["route_id"]
        return names

    def suggestions(self, headers: Any = None, **params: Any) -> Any:
        return self.client.get(SUGGESTIONS, params=params, headers=headers or self.athlete)

    def test_personalized_suggestions_are_ordered_with_reasons(self) -> None:
        """CA3: deportes del deportista, ciudad primero, luego distancia; con `reasons`."""
        self.seed()

        response = self.suggestions(
            sport=["running", "cycling"], city="medellín", country_code="CO"
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body = response.json()
        self.assertTrue(body["personalized"])
        self.assertEqual(
            [(item["name"], item["reasons"]) for item in body["items"]],
            [
                ("Medellín corta", ["SPORT_MATCH", "CITY_MATCH"]),
                ("Medellín larga", ["SPORT_MATCH", "CITY_MATCH"]),
                ("Ciclovía", ["SPORT_MATCH", "CITY_MATCH"]),
                ("Bogotá corta", ["SPORT_MATCH"]),
            ],
        )
        self.assertEqual(self.capabilities.calls, [(self.athlete_id, SUGGEST)])

    def test_without_sports_the_list_is_not_personalized(self) -> None:
        """CA3: sin deportes, las últimas publicadas, sin motivos y como mucho 20."""
        names = self.seed()

        response = self.suggestions(limit="50")

        body = response.json()
        self.assertFalse(body["personalized"])
        self.assertEqual(
            [item["route_id"] for item in body["items"]], list(reversed(list(names.values())))
        )
        self.assertTrue(all(item["reasons"] == [] for item in body["items"]))
        self.assertEqual(self.reader.suggest_queries[-1].limit, 20)

    def test_retired_routes_are_not_suggested(self) -> None:
        """CA4: una ruta retirada deja de sugerirse."""
        names = self.seed()
        self.retire(names["Medellín corta"])

        response = self.suggestions(sport=["running"], city="Medellín")

        self.assertNotIn(
            names["Medellín corta"], [item["route_id"] for item in response.json()["items"]]
        )

    def test_suggestions_require_athlete_and_capability(self) -> None:
        without = self.suggestions(headers=_bearer(uuid.uuid4(), "athlete"))
        organizer = self.suggestions(headers=self.organizer)

        self.assertEqual(
            (without.status_code, without.json()["code"]), (403, "capability_required")
        )
        self.assertEqual(
            (organizer.status_code, organizer.json()["code"]), (403, "permission_required")
        )
        self.capabilities.unavailable = True
        self.assertEqual(self.suggestions().status_code, 503)
        slow = self.client_with(_SlowCapabilities()).get(SUGGESTIONS, headers=self.athlete)
        self.assertEqual((slow.status_code, slow.json()["code"]), (504, "upstream_timeout"))

    def test_invalid_suggestion_filters_are_422_before_affiliation(self) -> None:
        cases = {
            "query.limit": {"limit": "51"},
            "query.city": {"city": "x"},
            "query.country_code": {"country_code": "ZZ"},
            "query.sport": {"sport": ["running", "running"]},
            "query.sport.0": {"sport": ["hiking"]},
        }
        for field, params in cases.items():
            with self.subTest(field=field):
                response = self.suggestions(**params)

                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["details"][0]["field"], field)
        self.assertEqual(self.capabilities.calls, [])


def _bearer(user_id: uuid.UUID, actor_type: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(user_id, actor_type)}"}


if __name__ == "__main__":
    unittest.main()
