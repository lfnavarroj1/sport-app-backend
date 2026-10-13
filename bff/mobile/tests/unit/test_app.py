import unittest

from fastapi.testclient import TestClient

from bff_mobile.main import API_VERSION, create_app
from bff_mobile.settings import BffMobileSettings

SETTINGS = BffMobileSettings(
    app_env="test",  # type: ignore[arg-type]
    users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
    affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
    training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
)


class BffMobileAppTest(unittest.TestCase):
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
        self.assertEqual(info["title"], "SportApp - BFF móvil")


if __name__ == "__main__":
    unittest.main()
