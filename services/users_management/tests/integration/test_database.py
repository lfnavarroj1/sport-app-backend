import unittest

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, pool

from sportapp_platform.migrations import current_revision, downgrade, upgrade
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from users_management.infrastructure.persistence_read.models import ReadBase
from users_management.infrastructure.persistence_write.models import WriteBase
from users_management.main import create_app
from users_management.settings import UsersManagementSettings

WRITE_HISTORY = "users_management.infrastructure.persistence_write:migrations"
READ_HISTORY = "users_management.infrastructure.persistence_read:migrations"


class DatabaseIntegrationTest(unittest.TestCase):
    def test_each_history_migrates_from_empty_database_to_head(self) -> None:
        expected = {
            WRITE_HISTORY: "0005_sport_habits",
            READ_HISTORY: "0004_user_directory_full_name",
        }
        for script_location, revision in expected.items():
            with (
                self.subTest(history=script_location),
                temporary_database("users_management_history") as url,
            ):
                upgrade(script_location, url)

                self.assertEqual(current_revision(url), revision)

    def test_migrations_match_orm_models(self) -> None:
        """Crear el ORM no equivale a una migración válida: ambos deben coincidir."""
        for script_location, metadata in (
            (WRITE_HISTORY, WriteBase.metadata),
            (READ_HISTORY, ReadBase.metadata),
        ):
            with (
                self.subTest(history=script_location),
                temporary_database("users_management_drift") as url,
            ):
                upgrade(script_location, url)
                engine = create_engine(url, poolclass=pool.NullPool)
                try:
                    with engine.connect() as connection:
                        differences = compare_metadata(
                            MigrationContext.configure(connection), metadata
                        )
                finally:
                    engine.dispose()

                self.assertEqual(differences, [])

    def test_each_history_downgrades_to_baseline(self) -> None:
        for history in (WRITE_HISTORY, READ_HISTORY):
            with (
                self.subTest(history=history),
                temporary_database("users_management_downgrade") as url,
            ):
                upgrade(history, url)

                downgrade(history, url, "0001_baseline")

                self.assertEqual(current_revision(url), "0001_baseline")

    def test_ready_against_real_postgres(self) -> None:
        with temporary_database("users_management_app") as url:
            settings = UsersManagementSettings(
                app_env="test",
                database_write_url=url,
                database_read_url=url,
                registration_policies_version="2026-10",
                health_data_consent_version="health-test.v1",
                jwt_private_key=ed25519_private_key_pem(),  # type: ignore[arg-type]
            )
            with TestClient(create_app(settings)) as client:
                response = client.get("/health/ready")

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
