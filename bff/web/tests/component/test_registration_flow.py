"""Componente HU001: BFF web → users_management → PostgreSQL (cuenta + outbox).

El BFF usa como transporte el cliente de pruebas del servicio real; la base es
PostgreSQL real migrada (requiere `TEST_DATABASE_URL`).
"""

import unittest
import uuid
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, pool, select

from bff_web.main import create_app as create_bff
from bff_web.settings import BffWebSettings
from sportapp_platform.correlation import CORRELATION_HEADER
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from users_management.infrastructure.persistence_write.models import (
    OutboxEventRow,
    UserAccountRow,
)
from users_management.main import create_app as create_users
from users_management.settings import UsersManagementSettings

WRITE_HISTORY = "users_management.infrastructure.persistence_write:migrations"
POLICIES = "2026-10"


def body(**overrides: Any) -> dict[str, Any]:
    values = {
        "full_name": "Ana Pérez",
        "email": "ana.componente@example.com",
        "password": "contraseña-muy-secreta",
        "actor_type": "organizer",
        "accepted_policies_version": POLICIES,
    }
    values.update(overrides)
    return values


class RegistrationFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        url = self.enterContext(temporary_database("bff_web_hu001"))
        upgrade(WRITE_HISTORY, url)
        users = TestClient(
            create_users(
                UsersManagementSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_write_url=url,  # type: ignore[arg-type]
                    database_read_url=url,  # type: ignore[arg-type]
                    registration_policies_version=POLICIES,
                    jwt_private_key=ed25519_private_key_pem(),  # type: ignore[arg-type]
                )
            ),
            base_url="http://users-management",
        )
        self.enterContext(users)
        settings = BffWebSettings(
            app_env="test",  # type: ignore[arg-type]
            users_management_base_url="http://users-management",  # type: ignore[arg-type]
            affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
            training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
            complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
            events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
        )
        self.bff = self.enterContext(TestClient(create_bff(settings, users_http=users)))
        self.engine = create_engine(url, poolclass=pool.NullPool)
        self.addCleanup(self.engine.dispose)

    def count(self, model: type[UserAccountRow | OutboxEventRow]) -> int:
        with self.engine.connect() as connection:
            return connection.scalar(select(func.count()).select_from(model)) or 0

    def test_register_through_bff_persists_account_and_event(self) -> None:
        correlation_id = str(uuid.uuid4())

        response = self.bff.post(
            "/v1/users",
            json=body(),
            headers={"Idempotency-Key": str(uuid.uuid4()), CORRELATION_HEADER: correlation_id},
        )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["actor_type"], "organizer")
        self.assertEqual(self.count(UserAccountRow), 1)
        with self.engine.connect() as connection:
            event = connection.execute(select(OutboxEventRow)).one()
        self.assertEqual(str(event.correlation_id), correlation_id)
        self.assertEqual(str(event.aggregate_id), response.json()["user_id"])

    def test_duplicate_and_retry_through_bff(self) -> None:
        key = str(uuid.uuid4())
        first = self.bff.post("/v1/users", json=body(), headers={"Idempotency-Key": key})

        retry = self.bff.post("/v1/users", json=body(), headers={"Idempotency-Key": key})
        duplicate = self.bff.post(
            "/v1/users",
            json=body(email="ANA.COMPONENTE@example.com"),
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )

        self.assertEqual(retry.status_code, 201)
        self.assertEqual(retry.json(), first.json())
        self.assertEqual(retry.headers["Idempotent-Replayed"], "true")
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["code"], "email_already_registered")
        self.assertEqual(self.count(UserAccountRow), 1)
        self.assertEqual(self.count(OutboxEventRow), 1)


if __name__ == "__main__":
    unittest.main()
