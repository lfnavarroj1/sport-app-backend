import os
import unittest
from unittest import mock

from pydantic import SecretStr

from sportapp_platform.config import (
    ConfigurationError,
    DatabasePoolSettings,
    PostgresUrl,
    ServiceSettings,
    load_settings,
)


class _SettingsWithDatabase(ServiceSettings, DatabasePoolSettings):
    database_url: PostgresUrl


class LoadSettingsTest(unittest.TestCase):
    def test_loads_valid_environment(self) -> None:
        environment = {
            "APP_ENV": "test",
            "DATABASE_URL": "postgresql+psycopg://app:clave-sintetica@db/app",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            settings = load_settings(_SettingsWithDatabase)

        self.assertEqual(settings.app_env, "test")
        self.assertEqual(settings.http_port, 8000)
        self.assertIsInstance(settings.database_url, SecretStr)
        self.assertNotIn("clave-sintetica", repr(settings))

    def test_missing_variable_fails_naming_only_the_variable(self) -> None:
        with mock.patch.dict(os.environ, {"APP_ENV": "test"}, clear=True):
            with self.assertRaises(ConfigurationError) as raised:
                load_settings(_SettingsWithDatabase)

        self.assertIn("DATABASE_URL", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_invalid_value_does_not_reveal_secret(self) -> None:
        environment = {"APP_ENV": "test", "DATABASE_URL": "mysql://app:clave-sintetica@db/app"}
        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(ConfigurationError) as raised:
                load_settings(_SettingsWithDatabase)

        self.assertIn("DATABASE_URL", str(raised.exception))
        self.assertNotIn("clave-sintetica", str(raised.exception))

    def test_rejects_unknown_environment(self) -> None:
        with mock.patch.dict(os.environ, {"APP_ENV": "qa"}, clear=True):
            with self.assertRaises(ConfigurationError) as raised:
                load_settings(ServiceSettings)

        self.assertIn("APP_ENV", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
