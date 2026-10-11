import unittest

from fastapi.testclient import TestClient

from bff_web.main import API_VERSION, create_app
from bff_web.settings import BffWebSettings

SETTINGS = BffWebSettings(app_env="test", users_management_base_url="http://users.invalid")


class BffWebAppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(create_app(SETTINGS))

    def test_live(self) -> None:
        response = self.client.get("/health/live")

        self.assertEqual(response.status_code, 200)

    def test_ready_without_external_dependencies(self) -> None:
        response = self.client.get("/health/ready")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "checks": {}})

    def test_openapi_declares_contract_version(self) -> None:
        info = self.client.get("/openapi.json").json()["info"]

        self.assertEqual(info["version"], API_VERSION)
        self.assertEqual(info["title"], "SportApp - BFF web")


if __name__ == "__main__":
    unittest.main()
