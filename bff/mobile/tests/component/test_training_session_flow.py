"""Componente HU032 en móvil: BFF → Usuarios (JWKS) → Entrenamientos → Afiliación (capacidad
al iniciar) → outbox → Pub/Sub → `entrenamientos-read-projector` → sesión activa e historial.

Servicios reales con PostgreSQL migrado (requiere `TEST_DATABASE_URL`). El tramo de
mensajería usa el emulador de Pub/Sub (`PUBSUB_EMULATOR_HOST`) con un proyecto propio.
"""

import contextlib
import os
import time
import unittest
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi.testclient import TestClient
from google.cloud import pubsub_v1
from sqlalchemy import create_engine, func, select
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
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from training_management.application.projections.snapshot_projection import TrainingProjector
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.messaging.pubsub import PubSubEventPublisher
from training_management.infrastructure.persistence_read.models import (
    ActiveTrainingSessionRow,
    TrainingSessionHistoryRow,
)
from training_management.infrastructure.persistence_read.plans import (
    SqlAlchemyProjectionUnitOfWork,
)
from training_management.infrastructure.persistence_write.models import OutboxEventRow
from training_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from training_management.main import create_app as create_training
from training_management.settings import (
    PROJECTOR_SUBSCRIPTION,
    TRAINING_TOPIC,
    OutboxPublisherSettings,
    TrainingManagementSettings,
)
from training_management.workers.outbox_publisher import OutboxRelay
from training_management.workers.pubsub_setup import ensure_resources
from training_management.workers.read_projector import ProjectionMessageHandler
from users_management.main import create_app as create_users
from users_management.settings import UsersManagementSettings

HISTORIES = {
    "users_w": "users_management.infrastructure.persistence_write:migrations",
    "users_r": "users_management.infrastructure.persistence_read:migrations",
    "affiliation": "affiliation_management.infrastructure.persistence:migrations",
    "training_w": "training_management.infrastructure.persistence_write:migrations",
    "training_r": "training_management.infrastructure.persistence_read:migrations",
}
POLICIES = "2026-10"
CATALOG = "plans-2026-10-13.v1"
PASSWORD = "contraseña-muy-secreta"


def utc_seconds(delta: timedelta, base: datetime | None = None) -> str:
    instant = (base or datetime.now(UTC)).replace(microsecond=0) + delta
    return instant.isoformat().replace("+00:00", "Z")


def wait_until(condition: Callable[[], bool], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


class TrainingSessionFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.urls = {
            name: self.enterContext(temporary_database(f"hu032_{name}")) for name in HISTORIES
        }
        for name, history in HISTORIES.items():
            upgrade(history, self.urls[name])
        self.users = self.enterContext(
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
                        health_data_consent_version="local-dev",
                    ),
                    users_http=self.users,
                    affiliation_http=affiliation,
                ),
                base_url="http://training-management",
            )
        )
        self.bff = self.enterContext(
            TestClient(
                create_bff(
                    BffMobileSettings(
                        app_env="test",  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                        training_management_base_url="http://training-management",  # type: ignore[arg-type]
                        events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                        complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
                        third_party_management_base_url="http://third-party.invalid",  # type: ignore[arg-type]
                    ),
                    users_http=self.users,
                    affiliation_http=affiliation,
                    training_http=training,
                ),
                base_url="https://bff-mobile",
            )
        )
        self.engines = {name: create_engine(url) for name, url in self.urls.items()}
        for engine in self.engines.values():
            self.addCleanup(engine.dispose)

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
            # Lo que haría el consumidor del alta gratuita (ADR-024): plan `FREE` activo.
            ProvisionFreeSubscription(
                unit_of_work=lambda: SqlAlchemyProvisioningUnitOfWork(
                    sessionmaker(self.engines["affiliation"])
                ),
                clock=AffiliationClock(),
            )(RegisteredUser(uuid.uuid4(), uuid.UUID(user_id), actor_type, None))
        login = self.users.post("/v1/sessions", json={"email": email, "password": PASSWORD})
        self.assertEqual(login.status_code, 200, login.text)
        return {"user_id": user_id, "Authorization": f"Bearer {login.json()['access_token']}"}

    def start(self, user: dict[str, str], body: dict[str, Any], key: str) -> httpx.Response:
        return self.bff.post(
            "/v1/training-sessions",
            json=body,
            headers={"Authorization": user["Authorization"], "Idempotency-Key": key},
        )

    def finish(self, user: dict[str, str], session_id: str, ended_at: str, key: str) -> Any:
        return self.bff.post(
            f"/v1/training-sessions/{session_id}/finish",
            json={"ended_at": ended_at},
            headers={"Authorization": user["Authorization"], "Idempotency-Key": key},
        )

    def count(self, name: str, model: Any) -> int:
        with sessionmaker(self.engines[name])() as session:
            return session.scalar(select(func.count()).select_from(model)) or 0

    def test_start_finish_and_retries_through_the_mobile_channel(self) -> None:
        """CA1-CA5 por el canal móvil."""
        athlete = self.register("athlete")
        session_id, start_key = str(uuid.uuid4()), str(uuid.uuid4())
        base = datetime.now(UTC)
        body = {
            "session_id": session_id,
            "sport": "running",
            "started_at": utc_seconds(-timedelta(minutes=30), base),
        }

        started = self.start(athlete, body, start_key)
        retried = self.start(athlete, body, start_key)
        second = self.start(athlete, {**body, "session_id": str(uuid.uuid4())}, str(uuid.uuid4()))

        self.assertEqual(started.status_code, 201, started.text)
        self.assertEqual(started.json()["athlete_id"], athlete["user_id"])
        self.assertEqual((started.json()["status"], started.json()["version"]), ("ACTIVE", 1))
        self.assertEqual(
            (retried.json(), retried.headers["Idempotent-Replayed"]), (started.json(), "true")
        )
        self.assertEqual(
            (second.status_code, second.json()["code"]), (409, "active_session_exists")
        )

        ended_at, finish_key = utc_seconds(-timedelta(minutes=5), base), str(uuid.uuid4())
        finished = self.finish(athlete, session_id, ended_at, finish_key)
        again = self.finish(athlete, session_id, ended_at, finish_key)
        stranger = self.finish(self.register("athlete"), session_id, ended_at, str(uuid.uuid4()))

        self.assertEqual(finished.status_code, 200, finished.text)
        self.assertEqual((finished.json()["status"], finished.json()["version"]), ("FINISHED", 2))
        self.assertEqual(finished.json()["duration_seconds"], 25 * 60)
        self.assertEqual(again.headers["Idempotent-Replayed"], "true")
        self.assertEqual(
            (stranger.status_code, stranger.json()["code"]), (404, "training_session_not_found")
        )
        self.assertEqual(self.count("training_w", OutboxEventRow), 2)

    def test_actor_and_capability_are_enforced(self) -> None:
        provider = self.register("service_provider")
        no_plan = self.register("athlete", free_plan=False)
        body = {
            "session_id": str(uuid.uuid4()),
            "sport": "running",
            "started_at": utc_seconds(timedelta()),
        }

        edge = self.start(provider, body, str(uuid.uuid4()))
        capability = self.start(no_plan, body, str(uuid.uuid4()))

        self.assertEqual((edge.status_code, edge.json()["code"]), (403, "permission_required"))
        self.assertEqual(
            (capability.status_code, capability.json()["code"]), (403, "capability_required")
        )
        self.assertEqual(self.count("training_w", OutboxEventRow), 0)

    @unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
    def test_session_reaches_active_view_and_history_through_pubsub(self) -> None:
        """CA8: activa mientras dura, después una sola entrada en el historial de HU011."""
        project_id = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        ensure_resources(project_id)
        athlete = self.register("athlete")
        session_id = str(uuid.uuid4())
        self.start(
            athlete,
            {
                "session_id": session_id,
                "sport": "cycling",
                "started_at": utc_seconds(-timedelta(minutes=50)),
            },
            str(uuid.uuid4()),
        )

        self.publish(project_id)
        self.assertTrue(
            self.consume(
                project_id, lambda: self.count("training_r", ActiveTrainingSessionRow) == 1
            )
        )
        active = self.bff.get(
            "/v1/training-sessions/active", headers={"Authorization": athlete["Authorization"]}
        )
        self.assertEqual((active.status_code, active.json()["session_id"]), (200, session_id))

        self.finish(athlete, session_id, utc_seconds(-timedelta(minutes=1)), str(uuid.uuid4()))
        self.publish(project_id)
        self.assertTrue(
            self.consume(
                project_id, lambda: self.count("training_r", TrainingSessionHistoryRow) == 1
            )
        )

        gone = self.bff.get(
            "/v1/training-sessions/active", headers={"Authorization": athlete["Authorization"]}
        )
        self.assertEqual(gone.status_code, 204)
        self.assertEqual(self.count("training_r", ActiveTrainingSessionRow), 0)

    def publish(self, project_id: str) -> None:
        publisher = PubSubEventPublisher(project_id, TRAINING_TOPIC)
        try:
            OutboxRelay(
                SqlAlchemyOutboxStore(sessionmaker(self.engines["training_w"])),
                publisher,
                OutboxPublisherSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_write_url=self.urls["training_w"],  # type: ignore[arg-type]
                    pubsub_project_id=project_id,
                    worker_id="w-test",
                ),
            ).run_once()
        finally:
            publisher.close()

    def consume(self, project_id: str, until: Callable[[], bool]) -> bool:
        subscriber = pubsub_v1.SubscriberClient()
        project = TrainingProjector(
            unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(
                sessionmaker(self.engines["training_r"])
            ),
            clock=SystemClock(),
        )
        streaming = subscriber.subscribe(
            subscriber.subscription_path(project_id, PROJECTOR_SUBSCRIPTION),
            callback=ProjectionMessageHandler(project),
        )
        try:
            return wait_until(until)
        finally:
            streaming.cancel()
            with contextlib.suppress(Exception):
                streaming.result(timeout=10)
            subscriber.close()


if __name__ == "__main__":
    unittest.main()
