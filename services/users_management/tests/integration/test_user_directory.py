"""HU010: lookup contra la base de lectura real (requiere `TEST_DATABASE_URL`).

El directorio lo mantiene `usuarios-read-projector` (refactor de HU001, creado con HU005).
Las pruebas del lookup siembran directamente la vista, igual que la dejaría el proyector;
`test_projected_registration_is_resolved_by_lookup` recorre el camino completo sin Pub/Sub
(registro → outbox → sobre publicado → proyector → lookup).
"""

import json
import unittest
import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from users_management.application.messages import parse_users_event
from users_management.application.projections.users_projection import (
    ProjectionOutcome,
    UsersProjector,
)
from users_management.infrastructure.clock import SystemClock
from users_management.infrastructure.persistence_read.models import UserDirectoryRow
from users_management.infrastructure.persistence_read.projection import (
    SqlAlchemyProjectionUnitOfWork,
)
from users_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from users_management.main import create_app
from users_management.settings import UsersManagementSettings
from users_management.workers.outbox_publisher import build_envelope

WRITE_HISTORY = "users_management.infrastructure.persistence_write:migrations"
READ_HISTORY = "users_management.infrastructure.persistence_read:migrations"


class UserDirectoryIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        write_url = self.enterContext(temporary_database("users_lookup_write"))
        read_url = self.enterContext(temporary_database("users_lookup_read"))
        upgrade(WRITE_HISTORY, write_url)
        upgrade(READ_HISTORY, read_url)
        engine = create_engine(read_url)
        self.addCleanup(engine.dispose)
        write_engine = create_engine(write_url)
        self.addCleanup(write_engine.dispose)
        self.read_sessions = sessionmaker(engine)
        self.write_sessions = sessionmaker(write_engine)
        self.active_id, self.disabled_id = uuid.uuid4(), uuid.uuid4()
        with sessionmaker(engine)() as session, session.begin():
            now = datetime.now(UTC)
            session.add_all(
                [
                    UserDirectoryRow(
                        user_id=self.active_id,
                        email="pro@example.com",
                        actor_type="service_provider",
                        status="active",
                        aggregate_version=1,
                        projected_at=now,
                    ),
                    UserDirectoryRow(
                        user_id=self.disabled_id,
                        email="baja@example.com",
                        actor_type="athlete",
                        status="disabled",
                        aggregate_version=2,
                        projected_at=now,
                    ),
                ]
            )
        self.client = self.enterContext(
            TestClient(
                create_app(
                    UsersManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_write_url=write_url,  # type: ignore[arg-type]
                        database_read_url=read_url,  # type: ignore[arg-type]
                        registration_policies_version="2026-10",
                        health_data_consent_version="health-test.v1",
                        jwt_private_key=ed25519_private_key_pem(),  # type: ignore[arg-type]
                    )
                )
            )
        )

    def test_lookup_reads_the_read_database(self) -> None:
        found = self.client.post("/v1/users/lookup", json={"email": "PRO@example.com"})
        disabled = self.client.post("/v1/users/lookup", json={"email": "baja@example.com"})
        missing = self.client.post("/v1/users/lookup", json={"email": "nadie@example.com"})

        self.assertEqual(
            found.json(), {"user_id": str(self.active_id), "actor_type": "service_provider"}
        )
        self.assertEqual(disabled.status_code, 404, "una cuenta deshabilitada no se resuelve")
        self.assertEqual(missing.status_code, 404)

    def test_registration_alone_does_not_fill_the_directory(self) -> None:
        """Sin `usuarios-read-projector` el registro no aparece en la lectura (deuda de HU001)."""
        registered = self.client.post(
            "/v1/users",
            json={
                "full_name": "Ana Pérez",
                "email": "ana@example.com",
                "password": "contraseña-muy-secreta",
                "actor_type": "athlete",
                "accepted_policies_version": "2026-10",
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        lookup = self.client.post("/v1/users/lookup", json={"email": "ana@example.com"})

        self.assertEqual(registered.status_code, 201)
        self.assertEqual(lookup.status_code, 404)

    def test_projected_registration_is_resolved_by_lookup(self) -> None:
        """Refactor de HU001: el sobre que publica la outbox llena `user_directory` y el
        lookup de HU010 resuelve al usuario recién registrado."""
        registered = self.client.post(
            "/v1/users",
            json={
                "full_name": "Ana Pérez",
                "email": "Ana.Nueva@example.com",
                "password": "contraseña-muy-secreta",
                "actor_type": "athlete",
                "accepted_policies_version": "2026-10",
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        claimed = SqlAlchemyOutboxStore(self.write_sessions).claim_batch("it-worker", 10, 30)
        project = UsersProjector(
            unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(self.read_sessions),
            clock=SystemClock(),
        )

        outcomes = [
            project(parse_users_event(json.loads(json.dumps(build_envelope(event)))))
            for event in claimed
        ]
        lookup = self.client.post("/v1/users/lookup", json={"email": "ana.nueva@example.com"})

        self.assertEqual(registered.status_code, 201)
        self.assertEqual(outcomes, [ProjectionOutcome.APPLIED])
        self.assertEqual(
            lookup.json(), {"user_id": registered.json()["user_id"], "actor_type": "athlete"}
        )


if __name__ == "__main__":
    unittest.main()
