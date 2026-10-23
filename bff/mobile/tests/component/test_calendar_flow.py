"""Componente HU035 en móvil: BFF → Usuarios (JWKS) → Afiliación (capacidad) → Eventos
(`GET /v1/events`, que vuelve a verificar la capacidad) y Entrenamientos
(`GET /v1/me/training-plan-sessions`) con PostgreSQL migrado (requiere `TEST_DATABASE_URL`).

El plan se asigna con las operaciones reales de HU010 y se proyecta con el proyector real.
La publicación de eventos pertenece a HU024 (todavía inexistente): el evento `PUBLISHED`
se inserta en `event_read`, junto con un borrador que nunca debe aparecer.
"""

import unittest
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from affiliation_management.application.commands.provision_free_subscription import (
    ProvisionFreeSubscription,
)
from affiliation_management.application.messages import RegisteredUser
from affiliation_management.infrastructure.clock import SystemClock as AffiliationClock
from affiliation_management.infrastructure.persistence.unit_of_work import (
    SqlAlchemyProvisioningUnitOfWork,
)
from affiliation_management.main import create_app as create_affiliation
from affiliation_management.settings import AffiliationManagementSettings
from bff_mobile.main import create_app as create_bff
from bff_mobile.settings import BffMobileSettings
from events_management.infrastructure.persistence_read.models import EventReadRow
from events_management.main import create_app as create_events
from events_management.settings import EventsManagementSettings
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from training_management.application.messages import parse_training_event
from training_management.application.projections.snapshot_projection import TrainingProjector
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.persistence_read.plans import (
    SqlAlchemyProjectionUnitOfWork,
)
from training_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from training_management.main import create_app as create_training
from training_management.settings import TrainingManagementSettings
from training_management.workers.outbox_publisher import build_envelope
from users_management.main import create_app as create_users
from users_management.settings import UsersManagementSettings

HISTORIES = {
    "users_w": "users_management.infrastructure.persistence_write:migrations",
    "users_r": "users_management.infrastructure.persistence_read:migrations",
    "affiliation": "affiliation_management.infrastructure.persistence:migrations",
    "training_w": "training_management.infrastructure.persistence_write:migrations",
    "training_r": "training_management.infrastructure.persistence_read:migrations",
    "events_w": "events_management.infrastructure.persistence_write:migrations",
    "events_r": "events_management.infrastructure.persistence_read:migrations",
}
POLICIES = "2026-10"
CATALOG = "plans-2026-10-13.v1"
PASSWORD = "contraseña-muy-secreta"
PERIOD = "from=2026-11-01T00:00:00-05:00&to=2026-11-30T23:59:59-05:00"


class CalendarFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.urls = {
            name: self.enterContext(temporary_database(f"hu035_{name}")) for name in HISTORIES
        }
        for name, history in HISTORIES.items():
            upgrade(history, self.urls[name])
        self.engines = {name: create_engine(url) for name, url in self.urls.items()}
        for engine in self.engines.values():
            self.addCleanup(engine.dispose)
        self.users = self.client(
            create_users(
                UsersManagementSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_write_url=self.urls["users_w"],  # type: ignore[arg-type]
                    database_read_url=self.urls["users_r"],  # type: ignore[arg-type]
                    registration_policies_version=POLICIES,
                    health_data_consent_version="health-test.v1",
                    jwt_private_key=ed25519_private_key_pem(),  # type: ignore[arg-type]
                )
            ),
            "http://users-management",
        )
        self.affiliation = self.client(
            create_affiliation(
                AffiliationManagementSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_url=self.urls["affiliation"],  # type: ignore[arg-type]
                    affiliation_expected_catalog_version=CATALOG,
                    users_management_base_url="http://users-management",  # type: ignore[arg-type]
                )
            ),
            "http://affiliation-management",
        )
        self.training = self.client(
            create_training(
                TrainingManagementSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_write_url=self.urls["training_w"],  # type: ignore[arg-type]
                    database_read_url=self.urls["training_r"],  # type: ignore[arg-type]
                    users_management_base_url="http://users-management",  # type: ignore[arg-type]
                    affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                ),
                users_http=self.users,
                affiliation_http=self.affiliation,
            ),
            "http://training-management",
        )
        self.events = self.client(
            create_events(
                EventsManagementSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_write_url=self.urls["events_w"],  # type: ignore[arg-type]
                    database_read_url=self.urls["events_r"],  # type: ignore[arg-type]
                    users_management_base_url="http://users-management",  # type: ignore[arg-type]
                    affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                ),
                users_http=self.users,
                affiliation_http=self.affiliation,
            ),
            "http://events-management",
        )
        self.bff = self.bff_with(self.events)

    def client(self, app: Any, base_url: str) -> TestClient:
        return self.enterContext(TestClient(app, base_url=base_url))

    def bff_with(self, events: httpx.Client) -> TestClient:
        return self.client(
            create_bff(
                BffMobileSettings(
                    app_env="test",  # type: ignore[arg-type]
                    users_management_base_url="http://users-management",  # type: ignore[arg-type]
                    affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                    training_management_base_url="http://training-management",  # type: ignore[arg-type]
                    events_management_base_url="http://events-management",  # type: ignore[arg-type]
                ),
                users_http=self.users,
                affiliation_http=self.affiliation,
                training_http=self.training,
                events_http=events,
            ),
            "https://bff-mobile",
        )

    def register(self, actor_type: str, *, free_plan: bool = True) -> dict[str, str]:
        email = f"{actor_type}-{uuid.uuid4().hex[:8]}@example.com"
        registered = self.users.post(
            "/v1/users",
            json={
                "full_name": "Persona Sintética",
                "email": email,
                "password": PASSWORD,
                "actor_type": actor_type,
                "accepted_policies_version": POLICIES,
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        user_id = registered.json()["user_id"]
        if actor_type == "athlete" and free_plan:
            ProvisionFreeSubscription(
                unit_of_work=lambda: SqlAlchemyProvisioningUnitOfWork(
                    sessionmaker(self.engines["affiliation"])
                ),
                clock=AffiliationClock(),
            )(RegisteredUser(uuid.uuid4(), uuid.UUID(user_id), actor_type, None))
        login = self.users.post("/v1/sessions", json={"email": email, "password": PASSWORD})
        return {"user_id": user_id, "Authorization": f"Bearer {login.json()['access_token']}"}

    def assign_plan(self, athlete: dict[str, str], professional: dict[str, str]) -> str:
        granted = self.training.put(
            f"/v1/training-authorizations/{professional['user_id']}",
            headers={"Authorization": athlete["Authorization"]},
        )
        self.assertIn(granted.status_code, (200, 201), granted.text)
        created = self.training.post(
            "/v1/training-plans",
            json={
                "athlete_id": athlete["user_id"],
                "title": "Base aeróbica noviembre",
                "objectives": ["Correr 10 km continuos"],
                "start_date": "2026-11-02",
                "end_date": "2026-12-06",
                "sessions": [
                    {
                        "scheduled_date": "2026-11-16",
                        "sport": "running",
                        "duration_minutes": 45,
                        "intensity_rpe": 4,
                    },
                    {
                        "scheduled_date": "2026-12-03",
                        "sport": "cycling",
                        "duration_minutes": 60,
                        "intensity_rpe": 5,
                    },
                ],
            },
            headers={
                "Authorization": professional["Authorization"],
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        store = SqlAlchemyOutboxStore(sessionmaker(self.engines["training_w"]))
        project = TrainingProjector(
            unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(
                sessionmaker(self.engines["training_r"])
            ),
            clock=SystemClock(),
        )
        for event in store.claim_batch("w-test", 100, 30):
            project(parse_training_event(build_envelope(event)))
            store.mark_published(event.event_id, "w-test")
        return str(created.json()["plan_id"])

    def add_event(self, starts_at: datetime, status: str) -> str:
        event_id = uuid.uuid4()
        now = datetime.now(UTC)
        with sessionmaker(self.engines["events_r"])() as session, session.begin():
            session.add(
                EventReadRow(
                    event_id=event_id,
                    organizer_id=uuid.uuid4(),
                    name="Carrera 10K Bogotá",
                    description="Carrera recreativa de diez kilómetros para aficionados.",
                    discipline="Running",
                    location_name="Parque Metropolitano Simón Bolívar",
                    location_address="Avenida Calle 63 # 60-50",
                    location_city="Bogotá",
                    location_country_code="CO",
                    starts_at=starts_at,
                    ends_at=starts_at + timedelta(hours=3),
                    time_zone="America/Bogota",
                    duration_minutes=180,
                    capacity=5000,
                    status=status,
                    version=2 if status == "PUBLISHED" else 1,
                    created_at=now,
                    updated_at=now,
                    projected_at=now,
                )
            )
        return str(event_id)

    def calendar(self, user: dict[str, str], query: str = PERIOD) -> httpx.Response:
        return self.bff.get(
            f"/v1/me/calendar?{query}", headers={"Authorization": user["Authorization"]}
        )

    def test_athlete_sees_published_events_and_own_planned_sessions(self) -> None:
        """CA1, CA3, CA5: publicado y propio sí; borrador, fuera de periodo y ajeno no."""
        athlete, professional = self.register("athlete"), self.register("service_provider")
        other = self.register("athlete")
        plan_id = self.assign_plan(athlete, professional)
        self.assign_plan(other, professional)
        published = self.add_event(datetime(2026, 11, 15, 12, tzinfo=UTC), "PUBLISHED")
        self.add_event(datetime(2026, 11, 20, 12, tzinfo=UTC), "DRAFT")

        response = self.calendar(athlete)

        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        self.assertEqual(
            [(item["source"], item["occurs_on"]) for item in items],
            [("EVENT_PUBLISHED", "2026-11-15"), ("TRAINING_PLAN_SESSION", "2026-11-16")],
        )
        self.assertEqual(items[0]["reference"], {"event_id": published})
        self.assertEqual(items[1]["reference"]["plan_id"], plan_id)
        self.assertEqual([source["status"] for source in response.json()["sources"]], ["OK", "OK"])

        later = self.add_event(datetime(2026, 11, 28, 12, tzinfo=UTC), "PUBLISHED")
        again = self.calendar(athlete)
        self.assertIn(later, [item["reference"].get("event_id") for item in again.json()["items"]])

    def test_capability_actor_and_partial_result(self) -> None:
        """CA5 y CA7."""
        athlete, professional = self.register("athlete"), self.register("service_provider")
        self.assign_plan(athlete, professional)
        no_plan = self.register("athlete", free_plan=False)

        self.assertEqual(self.calendar(no_plan).json()["code"], "capability_required")
        self.assertEqual(self.calendar(professional).json()["code"], "permission_required")

        def down(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("eventos caído")

        self.bff = self.bff_with(
            httpx.Client(base_url="http://events", transport=httpx.MockTransport(down))
        )
        partial = self.calendar(athlete)
        self.assertEqual(partial.status_code, 200)
        self.assertEqual(
            partial.json()["sources"],
            [
                {"name": "events", "status": "UNAVAILABLE", "truncated": False},
                {"name": "training", "status": "OK", "truncated": False},
            ],
        )
        self.assertEqual(
            [item["source"] for item in partial.json()["items"]], ["TRAINING_PLAN_SESSION"]
        )


if __name__ == "__main__":
    unittest.main()
