"""Componente HU026 (ADR-041): `bff-web` → `training-management` → outbox → Pub/Sub →
`entrenamientos-read-projector` → `route_view` → `bff-mobile` → sugerencias.

Servicios reales con PostgreSQL migrado (requiere `TEST_DATABASE_URL`) y el emulador de Pub/Sub
(`PUBSUB_EMULATOR_HOST`) con un proyecto propio para Entrenamientos. Usuarios proyecta su
directorio y el perfil del deportista sin Pub/Sub (ese transporte lo cubre su integración).
Mismo montaje que `test_training_metrics_flow.py` (se importa el módulo, no la clase, para no
volver a descubrir sus pruebas) más `bff-web` para el organizador.
"""

import json
import os
import unittest
import uuid
from typing import Any

import httpx
import test_training_metrics_flow as metrics_flow
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from affiliation_management.main import create_app as create_affiliation
from affiliation_management.settings import AffiliationManagementSettings
from bff_mobile.main import create_app as create_mobile
from bff_mobile.settings import BffMobileSettings
from bff_web.main import create_app as create_web
from bff_web.settings import BffWebSettings
from training_management.infrastructure.persistence_read.models import RouteViewRow
from training_management.main import create_app as create_training
from training_management.settings import TrainingManagementSettings
from training_management.workers.pubsub_setup import ensure_resources
from users_management.application.messages import parse_users_event
from users_management.application.projections.users_projection import UsersProjector
from users_management.infrastructure.clock import SystemClock as UsersClock
from users_management.infrastructure.persistence_read.projection import (
    SqlAlchemyProjectionUnitOfWork as UsersProjectionUnitOfWork,
)
from users_management.infrastructure.persistence_write.outbox import (
    SqlAlchemyOutboxStore as UsersOutboxStore,
)
from users_management.workers.outbox_publisher import build_envelope as users_envelope

SUGGESTIONS = "/v1/me/suggestions/routes"


def route_body(name: str, sport: str, city: str, distance_km: str) -> dict[str, Any]:
    return {
        "name": name,
        "sport": sport,
        "distance_km": distance_km,
        "difficulty": "MODERATE",
        "city": city,
        "country_code": "CO",
        "start_point": {"lat": 6.2442, "lng": -75.5812},
        "waypoints": [{"lat": 6.25, "lng": -75.57}],
    }


class RouteSuggestionsFlowTest(unittest.TestCase):
    register = metrics_flow.TrainingMetricsFlowTest.register
    publish = metrics_flow.TrainingMetricsFlowTest.publish
    consume = metrics_flow.TrainingMetricsFlowTest.consume

    def setUp(self) -> None:
        metrics_flow.TrainingMetricsFlowTest.setUp(self)  # type: ignore[arg-type]
        services = "http://affiliation-management"
        affiliation = self.enterContext(
            TestClient(
                create_affiliation(
                    AffiliationManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_url=self.urls["affiliation"],  # type: ignore[arg-type]
                        affiliation_expected_catalog_version=metrics_flow.CATALOG,
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                    )
                ),
                base_url=services,
            )
        )
        self.training = self.enterContext(
            TestClient(
                create_training(
                    TrainingManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_write_url=self.urls["training_w"],  # type: ignore[arg-type]
                        database_read_url=self.urls["training_r"],  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url=services,  # type: ignore[arg-type]
                    ),
                    users_http=self.users,
                    affiliation_http=affiliation,
                ),
                base_url="http://training-management",
            )
        )
        self.web = self.enterContext(
            TestClient(
                create_web(
                    BffWebSettings(
                        app_env="test",  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url=services,  # type: ignore[arg-type]
                        training_management_base_url="http://training-management",  # type: ignore[arg-type]
                        complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
                        events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                    ),
                    users_http=self.users,
                    affiliation_http=affiliation,
                    training_http=self.training,
                ),
                base_url="https://bff-web",
            )
        )
        users = self.users

        def users_down(request: httpx.Request) -> httpx.Response:
            # Solo responde el JWKS: el contexto y el directorio no están disponibles.
            if request.url.path == "/.well-known/jwks.json":
                return users.get(request.url.path)
            raise httpx.ConnectError("usuarios caído")

        self.mobile_without_users = self.enterContext(
            TestClient(
                create_mobile(
                    BffMobileSettings(
                        app_env="test",  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url=services,  # type: ignore[arg-type]
                        training_management_base_url="http://training-management",  # type: ignore[arg-type]
                        events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                    ),
                    users_http=httpx.Client(
                        base_url="http://users-management",
                        transport=httpx.MockTransport(users_down),
                    ),
                    affiliation_http=affiliation,
                    training_http=self.training,
                ),
                base_url="https://bff-mobile",
            )
        )

    def as_user(self, user: dict[str, str], **extra: str) -> dict[str, str]:
        return {"Authorization": user["Authorization"], **extra}

    def publish_route(self, organizer: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        response = self.web.post(
            "/v1/routes",
            json=body,
            headers=self.as_user(organizer, **{"Idempotency-Key": str(uuid.uuid4())}),
        )
        self.assertEqual(response.status_code, 201, response.text)
        created: dict[str, Any] = response.json()
        return created

    def profile(self, athlete: dict[str, str]) -> None:
        """HU005 y HU006 del deportista, directo en Usuarios (el canal web ya está probado)."""
        for path, body in (
            (
                "/v1/me/demographic-profile",
                {
                    "birth_date": "1992-04-18",
                    "gender": "FEMALE",
                    "country_code": "CO",
                    "city": "Medellín",
                    "marital_status": "SINGLE",
                    "occupation": "Ingeniera",
                    "income_range": "2_4_SMMLV",
                },
            ),
            (
                "/v1/me/sport-habits",
                {
                    "sports": [
                        {
                            "sport": "running",
                            "frequency_per_week": 3,
                            "session_duration_minutes": 45,
                            "intensity": "MODERATE",
                            "level": "INTERMEDIATE",
                        }
                    ],
                    "availability": {"days": ["SAT"], "time_slots": ["MORNING"]},
                    "primary_goal": "HEALTH",
                },
            ),
        ):
            response = self.users.put(
                path,
                json=body,
                headers=self.as_user(athlete, **{"Idempotency-Key": str(uuid.uuid4())}),
            )
            self.assertEqual(response.status_code, 201, response.text)

    def project_users(self) -> None:
        """`usuarios-outbox-publisher` + `usuarios-read-projector` sin Pub/Sub."""
        outbox = UsersOutboxStore(sessionmaker(self.engines["users_w"]))
        project = UsersProjector(
            unit_of_work=lambda: UsersProjectionUnitOfWork(sessionmaker(self.engines["users_r"])),
            clock=UsersClock(),
        )
        for event in outbox.claim_batch("w-test", 100, 30):
            project(parse_users_event(json.loads(json.dumps(users_envelope(event)))))
            outbox.mark_published(event.event_id, "w-test")

    def route_views(self, status: str | None = None) -> int:
        statement = select(func.count()).select_from(RouteViewRow)
        if status is not None:
            statement = statement.where(RouteViewRow.status == status)
        with sessionmaker(self.engines["training_r"])() as session:
            return session.scalar(statement) or 0

    @unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
    def test_published_routes_reach_the_athlete_and_retired_ones_do_not(self) -> None:
        """CA1, CA3 y CA4 de punta a punta; Usuarios caído → no personalizada (HA011)."""
        project_id = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        ensure_resources(project_id)
        organizer, other = self.register("organizer"), self.register("organizer")
        athlete, newcomer = self.register("athlete"), self.register("athlete")
        far = self.publish_route(organizer, route_body("Bogotá llana", "running", "Bogotá", "5"))
        near = self.publish_route(organizer, route_body("Embalse", "running", "Medellín", "10.0"))
        bike = self.publish_route(organizer, route_body("Ciclovía", "cycling", "Medellín", "30"))
        retired = self.publish_route(organizer, route_body("Cerrada", "running", "Medellín", "3"))
        foreign = self.web.post(
            f"/v1/routes/{retired['route_id']}/retire",
            json={"expected_version": 1},
            headers=self.as_user(other),
        )
        retire = self.web.post(
            f"/v1/routes/{retired['route_id']}/retire",
            json={"expected_version": 1},
            headers=self.as_user(organizer),
        )
        self.profile(athlete)
        self.project_users()

        self.publish(project_id)
        projected = self.consume(
            project_id, lambda: self.route_views() == 4 and self.route_views("RETIRED") == 1
        )

        self.assertEqual((foreign.status_code, foreign.json()["code"]), (404, "route_not_found"))
        self.assertEqual((retire.status_code, retire.json()["status"]), (200, "RETIRED"))
        self.assertTrue(projected, "el proyector no llevó las rutas a route_view")
        mine = self.web.get("/v1/me/routes", headers=self.as_user(organizer))
        self.assertEqual(
            [item["route_id"] for item in mine.json()["items"]],
            [retired["route_id"], bike["route_id"], near["route_id"], far["route_id"]],
        )
        self.assertEqual(mine.json()["items"][0]["status"], "RETIRED")

        response = self.bff.get(SUGGESTIONS, headers=self.as_user(athlete))
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(
            [(item["route_id"], item["reasons"]) for item in body["items"]],
            [
                (near["route_id"], ["SPORT_MATCH", "CITY_MATCH"]),
                (far["route_id"], ["SPORT_MATCH"]),
            ],
        )
        self.assertEqual({item["author_name"] for item in body["items"]}, {"Persona Sintética"})
        self.assertEqual((body["personalized"], body["missing_context"]), (True, []))
        self.assertEqual(body["sources"], [{"source": "users", "status": "OK"}])

        without_profile = self.bff.get(SUGGESTIONS, headers=self.as_user(newcomer)).json()
        self.assertEqual(
            (without_profile["personalized"], without_profile["missing_context"]),
            (False, ["sports", "city"]),
        )
        self.assertEqual(
            [item["route_id"] for item in without_profile["items"]],
            [bike["route_id"], near["route_id"], far["route_id"]],
        )

        degraded = self.mobile_without_users.get(SUGGESTIONS, headers=self.as_user(athlete))
        self.assertEqual(degraded.status_code, 200, degraded.text)
        self.assertFalse(degraded.json()["personalized"])
        self.assertEqual(degraded.json()["sources"], [{"source": "users", "status": "UNAVAILABLE"}])
        self.assertEqual(len(degraded.json()["items"]), 3)
        self.assertTrue(all(item["author_name"] is None for item in degraded.json()["items"]))

    def test_edge_and_validation_through_the_web_channel(self) -> None:
        """CA2 y `actor-permissions.v2`: ruta inválida → `422`; otros actores → `403`."""
        organizer, athlete = self.register("organizer"), self.register("athlete")
        provider = self.register("service_provider")

        invalid = self.web.post(
            "/v1/routes",
            json=route_body("Lejana", "running", "Cali", "500.1")
            | {"start_point": {"lat": 6.2, "lng": -190}},
            headers=self.as_user(organizer, **{"Idempotency-Key": str(uuid.uuid4())}),
        )
        out_of_range = self.web.post(
            "/v1/routes",
            json=route_body("Lejana", "running", "Cali", "500.1"),
            headers=self.as_user(organizer, **{"Idempotency-Key": str(uuid.uuid4())}),
        )
        forbidden = [
            self.web.post(
                "/v1/routes",
                json=route_body("Embalse", "running", "Medellín", "10"),
                headers=self.as_user(user, **{"Idempotency-Key": str(uuid.uuid4())}),
            )
            for user in (athlete, provider)
        ]

        self.assertEqual(invalid.status_code, 422)
        self.assertIn(
            {"field": "body.start_point.lng", "issue": "greater_than_equal"},
            invalid.json()["details"],
        )
        self.assertEqual(
            (out_of_range.status_code, out_of_range.json()["details"]),
            (422, [{"field": "body.distance_km", "issue": "out_of_range"}]),
        )
        for response in forbidden:
            self.assertEqual(
                (response.status_code, response.json()["code"]), (403, "permission_required")
            )
        self.assertEqual(self.route_views(), 0)
        with sessionmaker(self.engines["training_w"])() as session:
            self.assertEqual(
                session.scalar(select(func.count()).select_from(metrics_flow.OutboxEventRow)), 0
            )


if __name__ == "__main__":
    unittest.main()
