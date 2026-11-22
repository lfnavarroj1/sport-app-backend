"""Aplicación de `third_party_management` (HU031): salud, readiness y versión del contrato.

La base apunta a un puerto cerrado con timeout corto: el check `database` falla sin
conectarse a ninguna base real. Los valores de Strava son sintéticos.
"""

import base64
import os
import secrets
import unittest
from typing import Any

from fastapi.testclient import TestClient

from third_party_management.main import API_VERSION, create_app
from third_party_management.settings import ThirdPartyManagementSettings

BASE: dict[str, Any] = {
    "app_env": "test",
    "database_url": "postgresql+psycopg://contract@127.0.0.1:1/contract",
    "users_management_base_url": "http://users-management.invalid",
    "affiliation_management_base_url": "http://affiliation-management.invalid",
    "db_connect_timeout_seconds": 1,
}


def strava_values() -> dict[str, Any]:
    return {
        "strava_client_id": "4242",
        "strava_client_secret": secrets.token_urlsafe(24),
        "strava_token_key": base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
        "strava_redirect_uri": "https://bff-mobile.sportapp.test/v1/integrations/strava/callback",
        "strava_consent_version": "strava-consent-2026-10.v1",
    }


class ThirdPartyManagementAppTest(unittest.TestCase):
    def client(self, **overrides: Any) -> TestClient:
        return TestClient(create_app(ThirdPartyManagementSettings(**{**BASE, **overrides})))

    def test_live(self) -> None:
        response = self.client().get("/health/live")

        self.assertEqual(response.status_code, 200)

    def test_not_ready_without_database_nor_strava_configuration(self) -> None:
        client = self.client()

        with self.assertLogs("sportapp", level="WARNING") as logs:
            response = client.get("/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["checks"], {"database": "failed", "strava_configuration": "failed"}
        )
        messages = [record.getMessage() for record in logs.records]
        self.assertIn("database.unavailable", messages)
        self.assertIn("strava.configuration_incomplete", messages)

    def test_strava_configuration_passes_when_complete(self) -> None:
        client = self.client(**strava_values())

        with self.assertLogs("sportapp.database", level="WARNING"):
            response = client.get("/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["checks"], {"database": "failed", "strava_configuration": "ok"}
        )

    def test_readiness_logs_never_carry_strava_secrets(self) -> None:
        values = strava_values()
        client = self.client(**{**values, "strava_client_id": None})

        with self.assertLogs("sportapp", level="WARNING") as logs:
            client.get("/health/ready")

        text = " ".join(str(getattr(record, "fields", "")) for record in logs.records)
        self.assertIn("STRAVA_CLIENT_ID", text)
        self.assertNotIn(values["strava_client_secret"], text)
        self.assertNotIn(values["strava_token_key"], text)

    def test_openapi_declares_contract_version(self) -> None:
        info = self.client().get("/openapi.json").json()["info"]

        self.assertEqual(info["version"], API_VERSION)
        self.assertEqual(info["title"], "SportApp - Gestión de terceros")


if __name__ == "__main__":
    unittest.main()
