import unittest

from fastapi.testclient import TestClient

from sportapp_platform.migrations import current_revision, upgrade
from sportapp_platform.testing.postgres import temporary_database
from training_management.main import create_app
from training_management.settings import TrainingManagementSettings

HISTORIES = {
    "write": "training_management.infrastructure.persistence_write:migrations",
    "read": "training_management.infrastructure.persistence_read:migrations",
}


class DatabaseIntegrationTest(unittest.TestCase):
    def test_each_history_migrates_from_empty_database(self) -> None:
        for history, script_location in HISTORIES.items():
            with (
                self.subTest(history=history),
                temporary_database("training_management_" + history) as url,
            ):
                upgrade(script_location, url)

                self.assertEqual(current_revision(url), "0001_baseline")

    def test_ready_against_real_postgres(self) -> None:
        with temporary_database("training_management_app") as url:
            settings = TrainingManagementSettings(
                app_env="test",
                database_write_url=url,
                database_read_url=url,
            )
            with TestClient(create_app(settings)) as client:
                response = client.get("/health/ready")

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
