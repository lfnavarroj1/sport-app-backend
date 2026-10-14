import unittest

from fastapi.testclient import TestClient

from complementary_services.main import create_app
from complementary_services.settings import ComplementaryServicesSettings
from sportapp_platform.migrations import current_revision, upgrade
from sportapp_platform.testing.postgres import temporary_database

HISTORIES = {
    "main": "complementary_services.infrastructure.persistence:migrations",
}


class DatabaseIntegrationTest(unittest.TestCase):
    def test_each_history_migrates_from_empty_database(self) -> None:
        for history, script_location in HISTORIES.items():
            with (
                self.subTest(history=history),
                temporary_database("complementary_services_" + history) as url,
            ):
                upgrade(script_location, url)

                self.assertEqual(current_revision(url), "0003_service_publication")

    def test_ready_against_real_postgres(self) -> None:
        with temporary_database("complementary_services_app") as url:
            settings = ComplementaryServicesSettings(
                app_env="test",
                database_url=url,
                users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
            )
            with TestClient(create_app(settings)) as client:
                response = client.get("/health/ready")

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
