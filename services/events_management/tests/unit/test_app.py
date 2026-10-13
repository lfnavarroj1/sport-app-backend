import unittest

from fastapi.testclient import TestClient

from events_management.main import API_VERSION, create_app
from events_management.settings import EventsManagementSettings

SETTINGS = EventsManagementSettings(
    app_env="test",
    database_write_url="postgresql+psycopg://contract@127.0.0.1:1/contract",
    database_read_url="postgresql+psycopg://contract@127.0.0.1:1/contract",
    users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
    db_connect_timeout_seconds=1,
)


class EventsManagementAppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(create_app(SETTINGS))

    def test_live(self) -> None:
        response = self.client.get("/health/live")

        self.assertEqual(response.status_code, 200)

    def test_not_ready_without_database(self) -> None:
        with self.assertLogs("sportapp.database", level="WARNING"):
            response = self.client.get("/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["checks"], {"database_write": "failed"})

    def test_openapi_declares_contract_version(self) -> None:
        info = self.client.get("/openapi.json").json()["info"]

        self.assertEqual(info["version"], API_VERSION)
        self.assertEqual(info["title"], "SportApp - Eventos")


if __name__ == "__main__":
    unittest.main()
