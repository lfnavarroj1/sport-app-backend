import unittest

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, pool

from events_management.infrastructure.persistence_read.models import ReadBase
from events_management.infrastructure.persistence_write.models import WriteBase
from events_management.main import create_app
from events_management.settings import EventsManagementSettings
from sportapp_platform.migrations import current_revision, downgrade, upgrade
from sportapp_platform.testing.postgres import temporary_database

HISTORIES = {
    "write": (
        "events_management.infrastructure.persistence_write:migrations",
        "0002_event_draft",
    ),
    "read": (
        "events_management.infrastructure.persistence_read:migrations",
        "0003_event_read_calendar_index",
    ),
}


class DatabaseIntegrationTest(unittest.TestCase):
    def test_each_history_migrates_from_empty_database_and_back(self) -> None:
        for history, (script_location, head) in HISTORIES.items():
            with (
                self.subTest(history=history),
                temporary_database("events_management_" + history) as url,
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
                temporary_database("events_management_drift") as url,
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
        with temporary_database("events_management_app") as url:
            settings = EventsManagementSettings(
                app_env="test",
                database_write_url=url,
                database_read_url=url,
                users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
                affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
            )
            with TestClient(create_app(settings)) as client:
                response = client.get("/health/ready")

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
