import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from pydantic import ValidationError

from sportapp_platform.testing.keys import ed25519_private_key_pem
from users_management.main import API_VERSION, create_app
from users_management.settings import UsersManagementSettings

SETTINGS = UsersManagementSettings(
    app_env="test",
    database_write_url="postgresql+psycopg://contract@127.0.0.1:1/contract",
    database_read_url="postgresql+psycopg://contract@127.0.0.1:1/contract",
    registration_policies_version="2026-10",
    health_data_consent_version="health-test.v1",
    jwt_private_key=ed25519_private_key_pem(),  # type: ignore[arg-type]
    db_connect_timeout_seconds=1,
)


class SettingsTest(unittest.TestCase):
    def test_startup_is_rejected_without_the_health_consent_version(self) -> None:
        """HU005 (ADR-034): sin `HEALTH_DATA_CONSENT_VERSION` el servicio no arranca."""
        values = SETTINGS.model_dump()
        # pydantic-settings también lee el entorno: se aísla para que la ausencia sea real.
        environment = {k: v for k, v in os.environ.items() if k != "HEALTH_DATA_CONSENT_VERSION"}
        self.enterContext(mock.patch.dict(os.environ, environment, clear=True))
        for missing in (None, ""):
            with self.subTest(value=missing):
                values["health_data_consent_version"] = missing
                if missing is None:
                    del values["health_data_consent_version"]
                with self.assertRaises(ValidationError) as error:
                    UsersManagementSettings(**values)
                self.assertIn("health_data_consent_version", str(error.exception))


class UsersManagementAppTest(unittest.TestCase):
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
        self.assertEqual(info["title"], "SportApp - Usuarios")

    def test_profile_routes_are_wired_and_require_a_token(self) -> None:
        """HU005, HU006 y HU009: las cinco rutas del perfil exigen `Authorization: Bearer`."""
        self.assertEqual(API_VERSION, "0.15.0")
        for method, path in (
            ("PUT", "/v1/me/demographic-profile"),
            ("PUT", "/v1/me/physiological-profile"),
            ("PUT", "/v1/me/sport-habits"),
            ("PUT", "/v1/me/nutrition-habits"),
            ("GET", "/v1/me/profile"),
        ):
            with self.subTest(path=path):
                response = self.client.request(method, path, json={})

                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_athletes_search_is_wired_and_rejects_a_client_token(self) -> None:
        """HU039 (ADR-045): el contrato interno está montado y rechaza un token de cliente."""
        response = self.client.post(
            "/v1/users/athletes/search",
            json={"sports": ["running"], "city": "Bogotá", "country_code": "CO"},
            headers={"Authorization": "Bearer token-de-cliente"},
        )

        self.assertEqual(
            (response.status_code, response.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_account_routes_are_wired_and_require_a_token(self) -> None:
        """HU002: `PUT` y `GET /v1/me/account` exigen `Authorization: Bearer`."""
        for method in ("PUT", "GET"):
            with self.subTest(method=method):
                response = self.client.request(method, "/v1/me/account", json={})

                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main()
