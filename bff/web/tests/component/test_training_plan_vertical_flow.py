"""Cierre de HU010: recorrido vertical por Pub/Sub, sin sembrar datos a mano.

Servicios reales (Usuarios, Afiliación, Entrenamientos y BFF web) con PostgreSQL migrado
y el emulador de Pub/Sub; requiere `TEST_DATABASE_URL` y `PUBSUB_EMULATOR_HOST`. Cada
worker corre con su manejador real sobre su suscripción real:

- `usuarios-outbox-publisher` → `sportapp.usuarios.v1` → `usuarios-read-projector`
  (`user_directory`, que resuelve el lookup por correo) y el consumidor del alta gratuita
  de Afiliación (`FREE` con `training.plan.manage`);
- `entrenamientos-outbox-publisher` → `sportapp.entrenamientos.v1` →
  `entrenamientos-read-projector` (`training_plan_view`, consulta eventual).

A diferencia de `test_training_plan_flow.py`, aquí el directorio y la suscripción `FREE`
llegan por los eventos, como en la composición desplegada.
"""

import contextlib
import os
import time
import unittest
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from fastapi.testclient import TestClient
from google.cloud import pubsub_v1
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from affiliation_management.application.commands.provision_free_subscription import (
    ProvisionFreeSubscription,
)
from affiliation_management.infrastructure.clock import SystemClock as AffiliationClock
from affiliation_management.infrastructure.persistence.models import SubscriptionRow
from affiliation_management.infrastructure.persistence.unit_of_work import (
    SqlAlchemyProvisioningUnitOfWork,
)
from affiliation_management.main import create_app as create_affiliation
from affiliation_management.settings import (
    FREE_PLAN_SUBSCRIPTION,
    AffiliationManagementSettings,
)
from affiliation_management.workers.free_plan_consumer import FreePlanMessageHandler
from affiliation_management.workers.pubsub_setup import (
    ensure_resources as ensure_affiliation_resources,
)
from bff_web.main import create_app as create_bff
from bff_web.settings import BffWebSettings
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from training_management.application.projections.snapshot_projection import TrainingProjector
from training_management.infrastructure.clock import SystemClock as TrainingClock
from training_management.infrastructure.messaging.pubsub import (
    PubSubEventPublisher as TrainingPublisher,
)
from training_management.infrastructure.persistence_read.plans import (
    SqlAlchemyProjectionUnitOfWork as TrainingProjectionUnitOfWork,
)
from training_management.infrastructure.persistence_write.outbox import (
    SqlAlchemyOutboxStore as TrainingOutboxStore,
)
from training_management.main import create_app as create_training
from training_management.settings import (
    PROJECTOR_SUBSCRIPTION as TRAINING_SUBSCRIPTION,
)
from training_management.settings import (
    TRAINING_TOPIC,
    TrainingManagementSettings,
)
from training_management.settings import (
    OutboxPublisherSettings as TrainingOutboxSettings,
)
from training_management.workers.outbox_publisher import OutboxRelay as TrainingRelay
from training_management.workers.pubsub_setup import (
    ensure_resources as ensure_training_resources,
)
from training_management.workers.read_projector import (
    ProjectionMessageHandler as TrainingProjectionHandler,
)
from users_management.application.projections.users_projection import UsersProjector
from users_management.infrastructure.clock import SystemClock as UsersClock
from users_management.infrastructure.messaging.pubsub import (
    PubSubEventPublisher as UsersPublisher,
)
from users_management.infrastructure.persistence_read.models import UserDirectoryRow
from users_management.infrastructure.persistence_read.projection import (
    SqlAlchemyProjectionUnitOfWork as UsersProjectionUnitOfWork,
)
from users_management.infrastructure.persistence_write.outbox import (
    SqlAlchemyOutboxStore as UsersOutboxStore,
)
from users_management.main import create_app as create_users
from users_management.settings import PROJECTOR_SUBSCRIPTION as USERS_SUBSCRIPTION
from users_management.settings import USERS_TOPIC, UsersManagementSettings
from users_management.settings import OutboxPublisherSettings as UsersOutboxSettings
from users_management.workers.outbox_publisher import OutboxRelay as UsersRelay
from users_management.workers.pubsub_setup import ensure_resources as ensure_users_resources
from users_management.workers.read_projector import (
    ProjectionMessageHandler as UsersProjectionHandler,
)

USERS_WRITE = "users_management.infrastructure.persistence_write:migrations"
USERS_READ = "users_management.infrastructure.persistence_read:migrations"
AFFILIATION = "affiliation_management.infrastructure.persistence:migrations"
TRAINING_WRITE = "training_management.infrastructure.persistence_write:migrations"
TRAINING_READ = "training_management.infrastructure.persistence_read:migrations"
POLICIES = "2026-10"
CATALOG = "plans-2026-10-13.v1"
PASSWORD = "contraseña-muy-secreta"
ORIGIN = "http://localhost:4200"


def wait_until(condition: Callable[[], bool], timeout: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


def plan_request(athlete_email: str) -> dict[str, Any]:
    return {
        "athlete_email": athlete_email,
        "title": "Base aeróbica 10K",
        "objectives": ["Correr 10 km continuos"],
        "start_date": "2026-11-02",
        "end_date": "2026-11-29",
        "sessions": [
            {
                "scheduled_date": "2026-11-02",
                "sport": "running",
                "duration_minutes": 40,
                "intensity_rpe": 5,
            },
            {
                "scheduled_date": "2026-11-04",
                "sport": "strength",
                "duration_minutes": 30,
                "intensity_rpe": 6,
            },
        ],
    }


@unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
class TrainingPlanVerticalFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        names = ("users_w", "users_r", "affiliation", "training_w", "training_r")
        self.urls = {
            name: self.enterContext(temporary_database(f"hu010_vertical_{name}")) for name in names
        }
        for name, history in (
            ("users_w", USERS_WRITE),
            ("users_r", USERS_READ),
            ("affiliation", AFFILIATION),
            ("training_w", TRAINING_WRITE),
            ("training_r", TRAINING_READ),
        ):
            upgrade(history, self.urls[name])
        self.engines = {name: create_engine(url) for name, url in self.urls.items()}
        for engine in self.engines.values():
            self.addCleanup(engine.dispose)

        self.project = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        ensure_users_resources(self.project)
        ensure_affiliation_resources(self.project)
        ensure_training_resources(self.project)
        self.subscriber = pubsub_v1.SubscriberClient()
        self.addCleanup(self.subscriber.close)

        users = self.enterContext(
            TestClient(
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
                base_url="http://users-management",
            )
        )
        affiliation = self.enterContext(
            TestClient(
                create_affiliation(
                    AffiliationManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_url=self.urls["affiliation"],  # type: ignore[arg-type]
                        affiliation_expected_catalog_version=CATALOG,
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                    )
                ),
                base_url="http://affiliation-management",
            )
        )
        training = self.enterContext(
            TestClient(
                create_training(
                    TrainingManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_write_url=self.urls["training_w"],  # type: ignore[arg-type]
                        database_read_url=self.urls["training_r"],  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                    ),
                    users_http=users,
                    affiliation_http=affiliation,
                ),
                base_url="http://training-management",
            )
        )
        self.bff = self.enterContext(
            TestClient(
                create_bff(
                    BffWebSettings(
                        app_env="test",  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                        training_management_base_url="http://training-management",  # type: ignore[arg-type]
                        complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
                        events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                        cors_allowed_origins=(ORIGIN,),
                    ),
                    users_http=users,
                    affiliation_http=affiliation,
                    training_http=training,
                ),
                base_url="https://bff-web",
                headers={"Origin": ORIGIN},
            )
        )

    # --- Workers con su manejador real sobre su suscripción real ---

    @contextlib.contextmanager
    def workers(self) -> Any:
        users_projector = UsersProjector(
            unit_of_work=lambda: UsersProjectionUnitOfWork(sessionmaker(self.engines["users_r"])),
            clock=UsersClock(),
        )
        free_plan = ProvisionFreeSubscription(
            unit_of_work=lambda: SqlAlchemyProvisioningUnitOfWork(
                sessionmaker(self.engines["affiliation"])
            ),
            clock=AffiliationClock(),
        )
        training_projector = TrainingProjector(
            unit_of_work=lambda: TrainingProjectionUnitOfWork(
                sessionmaker(self.engines["training_r"])
            ),
            clock=TrainingClock(),
        )
        streams = [
            self.subscriber.subscribe(
                self.subscriber.subscription_path(self.project, subscription),
                callback=handler,
            )
            for subscription, handler in (
                (USERS_SUBSCRIPTION, UsersProjectionHandler(users_projector)),
                (FREE_PLAN_SUBSCRIPTION, FreePlanMessageHandler(free_plan)),
                (TRAINING_SUBSCRIPTION, TrainingProjectionHandler(training_projector)),
            )
        ]
        try:
            yield
        finally:
            for stream in streams:
                stream.cancel()
                # Tras cancelar, `result` puede relanzar la cancelación; solo se espera el cierre.
                with contextlib.suppress(Exception):
                    stream.result(timeout=10)

    def publish_users_outbox(self) -> int:
        publisher = UsersPublisher(self.project, USERS_TOPIC)
        try:
            return UsersRelay(
                UsersOutboxStore(sessionmaker(self.engines["users_w"])),
                publisher,
                UsersOutboxSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_write_url=self.urls["users_w"],  # type: ignore[arg-type]
                    pubsub_project_id=self.project,
                    worker_id="w-users",
                ),
            ).run_once()
        finally:
            publisher.close()

    def publish_training_outbox(self) -> int:
        publisher = TrainingPublisher(self.project, TRAINING_TOPIC)
        try:
            return TrainingRelay(
                TrainingOutboxStore(sessionmaker(self.engines["training_w"])),
                publisher,
                TrainingOutboxSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_write_url=self.urls["training_w"],  # type: ignore[arg-type]
                    pubsub_project_id=self.project,
                    worker_id="w-training",
                ),
            ).run_once()
        finally:
            publisher.close()

    def count(self, database: str, column: Any) -> int:
        with self.engines[database].connect() as connection:
            return connection.scalar(select(func.count()).select_from(column)) or 0

    # --- Clientes ---

    def register(self, actor_type: str) -> dict[str, str]:
        email = f"{actor_type}-{uuid.uuid4().hex[:8]}@example.com"
        registered = self.bff.post(
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
        session = self.bff.post("/v1/sessions", json={"email": email, "password": PASSWORD})
        self.assertEqual(session.status_code, 200, session.text)
        return {
            "user_id": registered.json()["user_id"],
            "email": email,
            "token": session.json()["access_token"],
        }

    def auth(self, user: dict[str, str], **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {user['token']}", **extra}

    def authorize(self, athlete: dict[str, str], professional: dict[str, str]) -> httpx.Response:
        return self.bff.post(
            "/v1/me/training-authorizations",
            json={"professional_email": professional["email"]},
            headers=self.auth(athlete),
        )

    def create_plan(self, professional: dict[str, str], athlete_email: str, key: str) -> Any:
        return self.bff.post(
            "/v1/training-plans",
            json=plan_request(athlete_email),
            headers=self.auth(professional, **{"Idempotency-Key": key}),
        )

    def read_plan(self, user: dict[str, str], plan_id: str) -> httpx.Response:
        return self.bff.get(f"/v1/training-plans/{plan_id}", headers=self.auth(user))

    def test_vertical_flow_through_pub_sub(self) -> None:
        """HU010 CA1, CA2, CA4 y CA5 con lookup real, capacidad real y proyección real."""
        athlete = self.register("athlete")
        professional = self.register("service_provider")
        outsider = self.register("service_provider")

        with self.workers():
            # Registro → outbox → Pub/Sub → directorio de Usuarios y alta FREE de Afiliación.
            self.assertEqual(self.publish_users_outbox(), 3)
            self.assertTrue(
                wait_until(
                    lambda: (
                        self.count("users_r", UserDirectoryRow) == 3
                        and self.count("affiliation", SubscriptionRow) == 1
                    )
                ),
                "el proyector de Usuarios o el alta FREE no procesaron el registro",
            )

            # Lookup real por correo + capacidad `training.plan.manage` del plan FREE.
            granted = self.authorize(athlete, professional)
            self.assertEqual(granted.status_code, 201, granted.text)
            self.assertEqual(granted.json()["professional_id"], professional["user_id"])

            # CA2: un profesional sin autorización no puede asignarle planes.
            forbidden = self.create_plan(outsider, athlete["email"], str(uuid.uuid4()))
            self.assertEqual(forbidden.status_code, 403, forbidden.text)
            self.assertEqual(forbidden.json()["code"], "athlete_authorization_required")

            # CA1 y CA5: creación idempotente.
            key = str(uuid.uuid4())
            created = self.create_plan(professional, athlete["email"], key)
            replayed = self.create_plan(professional, athlete["email"], key)
            self.assertEqual(created.status_code, 201, created.text)
            self.assertEqual(replayed.status_code, 201, replayed.text)
            self.assertEqual(replayed.headers.get("Idempotent-Replayed"), "true")
            plan_id = created.json()["plan_id"]
            self.assertEqual(replayed.json()["plan_id"], plan_id)
            self.assertEqual(created.json()["athlete_id"], athlete["user_id"])

            # CA4: la consulta es eventual; antes de proyectar responde `404`.
            self.assertEqual(self.read_plan(professional, plan_id).status_code, 404)

            # Autorización y plan → outbox → Pub/Sub → `training_plan_view`.
            self.assertGreaterEqual(self.publish_training_outbox(), 2)
            self.assertTrue(
                wait_until(lambda: self.read_plan(professional, plan_id).status_code == 200),
                "el proyector de Entrenamientos no proyectó el plan",
            )

        by_creator = self.read_plan(professional, plan_id)
        by_athlete = self.read_plan(athlete, plan_id)
        by_outsider = self.read_plan(outsider, plan_id)
        self.assertEqual(by_creator.json()["plan_id"], plan_id)
        self.assertEqual(by_creator.json()["athlete_id"], athlete["user_id"])
        self.assertEqual(by_creator.json()["professional_id"], professional["user_id"])
        self.assertEqual(len(by_creator.json()["sessions"]), 2)
        self.assertEqual(by_athlete.status_code, 200, by_athlete.text)
        self.assertEqual(by_outsider.status_code, 404, by_outsider.text)


if __name__ == "__main__":
    unittest.main()
