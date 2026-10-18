"""HU010: lookup contra la base de lectura real (requiere `TEST_DATABASE_URL`).

El directorio lo mantiene `usuarios-read-projector` (refactor de HU001, creado con HU005).
Las pruebas del lookup siembran directamente la vista, igual que la dejaría el proyector;
`test_projected_registration_is_resolved_by_lookup` recorre el camino completo sin Pub/Sub
(registro → outbox → sobre publicado → proyector → lookup).

HU011 (CA5): `POST /v1/users/lookup-batch` lee nombre y correo del mismo directorio; el
nombre lo guarda el proyector y, para las filas anteriores a `0004`, el relleno único.
"""

import json
import unittest
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, update
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
from users_management.workers.directory_backfill import backfill
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
        self.read_engine, self.write_engine = engine, write_engine
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
                        full_name="Pro Sintético",
                        actor_type="service_provider",
                        status="active",
                        aggregate_version=1,
                        projected_at=now,
                    ),
                    UserDirectoryRow(
                        user_id=self.disabled_id,
                        email="baja@example.com",
                        full_name="Baja Sintética",
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

    # --- HU011 (CA5) ---

    def register_and_project(self, full_name: str, email: str) -> str:
        registered = self.client.post(
            "/v1/users",
            json={
                "full_name": full_name,
                "email": email,
                "password": "contraseña-muy-secreta",
                "actor_type": "athlete",
                "accepted_policies_version": "2026-10",
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(registered.status_code, 201)
        project = UsersProjector(
            unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(self.read_sessions),
            clock=SystemClock(),
        )
        for event in SqlAlchemyOutboxStore(self.write_sessions).claim_batch("it", 10, 30):
            project(parse_users_event(json.loads(json.dumps(build_envelope(event)))))
        user_id: str = registered.json()["user_id"]
        return user_id

    def batch(self, *user_ids: object) -> Any:
        return self.client.post(
            "/v1/users/lookup-batch", json={"user_ids": [str(u) for u in user_ids]}
        )

    def test_batch_returns_projected_names_of_active_accounts(self) -> None:
        athlete_id = self.register_and_project("Ana Pérez", "ana.batch@example.com")
        unknown = uuid.uuid4()

        response = self.batch(athlete_id, self.disabled_id, unknown, self.active_id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["items"],
            [
                {
                    "user_id": athlete_id,
                    "full_name": "Ana Pérez",
                    "email": "ana.batch@example.com",
                },
                {
                    "user_id": str(self.active_id),
                    "full_name": "Pro Sintético",
                    "email": "pro@example.com",
                },
            ],
        )

    def test_rows_without_name_are_omitted_until_the_backfill_fills_them(self) -> None:
        """Filas proyectadas antes de `0004`: sin nombre no se entregan; el relleno las
        completa desde `user_account` una sola vez y no toca las que ya tienen nombre."""
        athlete_id = self.register_and_project("Luis Gómez", "luis.batch@example.com")
        with self.read_sessions() as session, session.begin():
            session.execute(
                update(UserDirectoryRow)
                .where(UserDirectoryRow.user_id == uuid.UUID(athlete_id))
                .values(full_name=None)
            )

        before = self.batch(athlete_id)
        first = backfill(self.write_engine, self.read_engine)
        second = backfill(self.write_engine, self.read_engine)
        after = self.batch(athlete_id, self.active_id)

        self.assertEqual(before.json()["items"], [])
        self.assertEqual((first, second), (1, 0))
        self.assertEqual(
            [item["full_name"] for item in after.json()["items"]],
            ["Luis Gómez", "Pro Sintético"],
        )


if __name__ == "__main__":
    unittest.main()
