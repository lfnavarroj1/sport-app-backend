import unittest

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, pool

from sportapp_platform.migrations import current_revision, downgrade, upgrade
from sportapp_platform.testing.postgres import temporary_database
from training_management.infrastructure.persistence_read.models import ReadBase
from training_management.infrastructure.persistence_write.models import WriteBase
from training_management.main import create_app
from training_management.settings import TrainingManagementSettings

HISTORIES = {
    "write": (
        "training_management.infrastructure.persistence_write:migrations",
        "0007_routes",
    ),
    "read": (
        "training_management.infrastructure.persistence_read:migrations",
        "0009_route_view",
    ),
}


class DatabaseIntegrationTest(unittest.TestCase):
    def test_each_history_migrates_from_empty_database_and_back(self) -> None:
        for history, (script_location, head) in HISTORIES.items():
            with (
                self.subTest(history=history),
                temporary_database("training_management_" + history) as url,
            ):
                upgrade(script_location, url)
                self.assertEqual(current_revision(url), head)

                downgrade(script_location, url, "0001_baseline")
                self.assertEqual(current_revision(url), "0001_baseline")

                upgrade(script_location, url)
                self.assertEqual(current_revision(url), head)

    def test_migrations_match_orm_models(self) -> None:
        """Crear el ORM no equivale a una migración válida: ambos deben coincidir."""
        for (script_location, _), metadata in (
            (HISTORIES["write"], WriteBase.metadata),
            (HISTORIES["read"], ReadBase.metadata),
        ):
            with (
                self.subTest(history=script_location),
                temporary_database("training_management_drift") as url,
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

    def test_ready_against_real_postgres(self) -> None:
        with temporary_database("training_management_app") as url:
            settings = TrainingManagementSettings(
                app_env="test",
                database_write_url=url,
                database_read_url=url,
                users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
                affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
            )
            with TestClient(create_app(settings)) as client:
                # HU014: sin el catálogo de alimentos migrado el servicio no queda listo.
                with self.assertLogs("sportapp.catalog", level="WARNING"):
                    before = client.get("/health/ready")
                upgrade(HISTORIES["write"][0], url)
                response = client.get("/health/ready")

        self.assertEqual(before.status_code, 503)
        self.assertEqual(
            before.json()["checks"], {"database_write": "ok", "food_catalog": "failed"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["checks"], {"database_write": "ok", "food_catalog": "ok"})


if __name__ == "__main__":
    unittest.main()
