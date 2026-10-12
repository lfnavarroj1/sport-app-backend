"""HU010: lookup contra la base de lectura real (requiere `TEST_DATABASE_URL`).

El directorio lo mantendrá `usuarios-read-projector` (refactor pendiente de HU001);
aquí se siembra directamente la vista de lectura, igual que la dejaría el proyector.
"""

import unittest
import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from users_management.infrastructure.persistence_read.models import UserDirectoryRow
from users_management.main import create_app
from users_management.settings import UsersManagementSettings

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


if __name__ == "__main__":
    unittest.main()
