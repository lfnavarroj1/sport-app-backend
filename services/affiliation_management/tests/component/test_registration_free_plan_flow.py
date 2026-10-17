"""Componente HU001 (ADR-024): registro → Pub/Sub → Afiliación → suscripción `FREE`.

Recorrido real: `bff-web` → `users-management` → PostgreSQL de Usuarios → outbox →
`usuarios-outbox-publisher` → emulador de Pub/Sub → consumidor del alta gratuita →
PostgreSQL de Afiliación → contrato interno de capacidades.

Requiere `TEST_DATABASE_URL` y `PUBSUB_EMULATOR_HOST`.
"""

import contextlib
import os
import time
import unittest
import uuid
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from google.cloud import pubsub_v1
from sqlalchemy import create_engine, pool
from sqlalchemy.orm import sessionmaker

from affiliation_management.application.commands.provision_free_subscription import (
    ProvisionFreeSubscription,
)
from affiliation_management.infrastructure.clock import SystemClock
from affiliation_management.infrastructure.persistence.unit_of_work import (
    SqlAlchemyProvisioningUnitOfWork,
)
from affiliation_management.main import create_app as create_affiliation
from affiliation_management.settings import FREE_PLAN_SUBSCRIPTION, AffiliationManagementSettings
from affiliation_management.workers.free_plan_consumer import FreePlanMessageHandler
from affiliation_management.workers.pubsub_setup import ensure_resources
from bff_web.main import create_app as create_bff
from bff_web.settings import BffWebSettings
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from users_management.infrastructure.messaging.pubsub import PubSubEventPublisher
from users_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from users_management.main import create_app as create_users
from users_management.settings import USERS_TOPIC, OutboxPublisherSettings, UsersManagementSettings
from users_management.workers.outbox_publisher import OutboxRelay

USERS_HISTORY = "users_management.infrastructure.persistence_write:migrations"
AFFILIATION_HISTORY = "affiliation_management.infrastructure.persistence:migrations"
POLICIES = "2026-10"
CATALOG = "plans-2026-10-11.v1"


def wait_until(condition: Callable[[], bool], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


@unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
class RegistrationFreePlanFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project = f"sportapp-e2e-{uuid.uuid4().hex[:8]}"
        ensure_resources(self.project)
        users_url = self.enterContext(temporary_database("hu001_users"))
        affiliation_url = self.enterContext(temporary_database("hu001_affiliation"))
        upgrade(USERS_HISTORY, users_url)
        upgrade(AFFILIATION_HISTORY, affiliation_url)

        users = self.enterContext(
            TestClient(
                create_users(
                    UsersManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_write_url=users_url,  # type: ignore[arg-type]
                        database_read_url=users_url,  # type: ignore[arg-type]
                        registration_policies_version=POLICIES,
                        health_data_consent_version="health-test.v1",
                        jwt_private_key=ed25519_private_key_pem(),  # type: ignore[arg-type]
                    )
                ),
                base_url="http://users-management",
            )
        )
        self.bff = self.enterContext(
            TestClient(
                create_bff(
                    BffWebSettings(
                        app_env="test",  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
                        training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
                        complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
                        events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                    ),
                    users_http=users,
                )
            )
        )
        self.affiliation = self.enterContext(
            TestClient(
                create_affiliation(
                    AffiliationManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_url=affiliation_url,  # type: ignore[arg-type]
                        affiliation_expected_catalog_version=CATALOG,
                    )
                )
            )
        )

        users_engine = create_engine(users_url, poolclass=pool.NullPool)
        self.addCleanup(users_engine.dispose)
        publisher = PubSubEventPublisher(self.project, USERS_TOPIC)
        self.addCleanup(publisher.close)
        self.users_relay = OutboxRelay(
            SqlAlchemyOutboxStore(sessionmaker(users_engine)),
            publisher,
            OutboxPublisherSettings(
                app_env="test",  # type: ignore[arg-type]
                database_write_url=users_url,  # type: ignore[arg-type]
                pubsub_project_id=self.project,
                worker_id="users-relay",
            ),
        )

        affiliation_engine = create_engine(affiliation_url, pool_size=10)
        self.addCleanup(affiliation_engine.dispose)
        provision = ProvisionFreeSubscription(
            unit_of_work=lambda: SqlAlchemyProvisioningUnitOfWork(sessionmaker(affiliation_engine)),
            clock=SystemClock(),
        )
        subscriber = pubsub_v1.SubscriberClient()
        self.addCleanup(subscriber.close)
        streaming = subscriber.subscribe(
            subscriber.subscription_path(self.project, FREE_PLAN_SUBSCRIPTION),
            callback=FreePlanMessageHandler(provision),
        )

        def stop_consumer() -> None:
            streaming.cancel()
            with contextlib.suppress(Exception):
                streaming.result(timeout=10)

        self.addCleanup(stop_consumer)

    def register(self, actor_type: str) -> str:
        response = self.bff.post(
            "/v1/users",
            json={
                "full_name": "Ana Pérez",
                "email": f"ana-{uuid.uuid4().hex[:8]}@example.com",
                "password": "contraseña-muy-secreta",
                "actor_type": actor_type,
                "accepted_policies_version": POLICIES,
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return str(response.json()["user_id"])

    def capabilities(self, user_id: str) -> dict[str, Any]:
        response = self.affiliation.get(f"/v1/subscriptions/by-user/{user_id}/capabilities")
        self.assertEqual(response.status_code, 200)
        return dict(response.json())

    def test_registered_athlete_ends_with_free_plan_and_organizer_with_none(self) -> None:
        """CA6, CA7 y el lado de Afiliación de CA8 de punta a punta."""
        athlete = self.register("athlete")
        organizer = self.register("organizer")

        self.assertEqual(self.capabilities(athlete)["subscription_status"], "NONE")
        self.assertEqual(self.users_relay.run_once(), 2)

        provisioned = wait_until(
            lambda: self.capabilities(athlete)["subscription_status"] == "ACTIVE"
        )

        self.assertTrue(provisioned, "la suscripción FREE no se observó a tiempo")
        athlete_view = self.capabilities(athlete)
        self.assertEqual(athlete_view["plan_code"], "FREE")
        self.assertEqual(athlete_view["catalog_version"], CATALOG)
        self.assertEqual(len(athlete_view["capabilities"]), 11)
        self.assertEqual(self.capabilities(organizer)["subscription_status"], "NONE")


if __name__ == "__main__":
    unittest.main()
