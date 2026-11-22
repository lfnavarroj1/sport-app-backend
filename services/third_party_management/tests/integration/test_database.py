"""Migración `0001_strava_connections` y readiness contra PostgreSQL real (HU031, ADR-042 §2)."""

import unittest

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, pool
from support import HEAD, HISTORY, settings_for

from sportapp_platform.migrations import current_revision, downgrade, upgrade
from sportapp_platform.testing.postgres import temporary_database
from third_party_management.infrastructure.persistence.models import Base
from third_party_management.main import create_app

TABLES = {
    "external_account",
    "oauth_state",
    "sync_run",
    "imported_activity",
    "idempotency_key",
    "outbox_event",
}


def tables_of(url: str) -> set[str]:
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        return set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()


class MigrationTest(unittest.TestCase):
    def test_migrates_from_empty_database_to_head_and_back(self) -> None:
        with temporary_database("third_party_head") as url:
            upgrade(HISTORY, url)
            self.assertEqual(current_revision(url), HEAD)
            self.assertEqual(tables_of(url), TABLES)

            downgrade(HISTORY, url, "base")
            self.assertIsNone(current_revision(url))
            self.assertEqual(tables_of(url), set())

            upgrade(HISTORY, url)
            self.assertEqual(current_revision(url), HEAD)
            self.assertEqual(tables_of(url), TABLES)

    def test_migration_matches_orm_models(self) -> None:
        """Crear el ORM no equivale a una migración válida: ambos deben coincidir."""
        with temporary_database("third_party_drift") as url:
            upgrade(HISTORY, url)
            engine = create_engine(url, poolclass=pool.NullPool)
            try:
                with engine.connect() as connection:
                    differences = compare_metadata(
                        MigrationContext.configure(connection), Base.metadata
                    )
            finally:
                engine.dispose()

        self.assertEqual(differences, [])


class ReadinessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.url = self.enterContext(temporary_database("third_party_ready"))
        upgrade(HISTORY, self.url)

    def test_ready_with_database_and_complete_strava_configuration(self) -> None:
        with TestClient(create_app(settings_for(self.url))) as client:
            response = client.get("/health/ready")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["checks"], {"database": "ok", "strava_configuration": "ok"}
        )

    def test_not_ready_without_strava_configuration(self) -> None:
        with TestClient(create_app(settings_for(self.url, configured=False))) as client:
            with self.assertLogs("sportapp.strava", level="WARNING") as logs:
                response = client.get("/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["checks"], {"database": "ok", "strava_configuration": "failed"}
        )
        # Solo los nombres de las variables, nunca sus valores.
        self.assertEqual(
            logs.records[0].fields["missing"],  # type: ignore[attr-defined]
            [
                "STRAVA_CLIENT_ID",
                "STRAVA_CLIENT_SECRET",
                "STRAVA_REDIRECT_URI",
                "STRAVA_CONSENT_VERSION",
                "STRAVA_TOKEN_KEY",
            ],
        )


if __name__ == "__main__":
    unittest.main()
