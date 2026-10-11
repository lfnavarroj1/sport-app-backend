import os
import unittest
from unittest import mock

from sportapp_platform import migrations
from sportapp_platform.config import ConfigurationError
from sportapp_platform.serve import DEFAULT_PORT, resolve_server_options


class ResolveServerOptionsTest(unittest.TestCase):
    def test_uses_factory_and_default_port(self) -> None:
        factory, port = resolve_server_options({"APP_FACTORY": "pkg.main:create_app"})

        self.assertEqual(factory, "pkg.main:create_app")
        self.assertEqual(port, DEFAULT_PORT)

    def test_requires_factory(self) -> None:
        with self.assertRaises(ConfigurationError):
            resolve_server_options({})

    def test_rejects_invalid_port(self) -> None:
        with self.assertRaises(ConfigurationError):
            resolve_server_options({"APP_FACTORY": "pkg.main:create_app", "HTTP_PORT": "abc"})


class MigrationsCommandTest(unittest.TestCase):
    def test_upgrade_reads_url_from_named_variable(self) -> None:
        url = "postgresql+psycopg://migrator:clave-sintetica@db/app"
        with (
            mock.patch.dict(os.environ, {"DATABASE_WRITE_URL": url}, clear=True),
            mock.patch.object(migrations.command, "upgrade") as upgrade,
        ):
            migrations.main(
                [
                    "upgrade",
                    "--script-location",
                    "pkg:migrations",
                    "--url-env",
                    "DATABASE_WRITE_URL",
                ]
            )

        config, revision = upgrade.call_args.args
        self.assertEqual(revision, "head")
        self.assertEqual(config.get_main_option("script_location"), "pkg:migrations")
        self.assertEqual(config.attributes["database_url"], url)
        self.assertIsNone(config.get_main_option("sqlalchemy.url"))

    def test_upgrade_fails_when_variable_is_missing(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigurationError) as raised:
                migrations.main(
                    ["upgrade", "--script-location", "pkg:migrations", "--url-env", "DB_URL"]
                )

        self.assertIn("DB_URL", str(raised.exception))

    def test_offline_mode_is_rejected(self) -> None:
        context = mock.Mock()
        context.is_offline_mode.return_value = True

        with self.assertRaises(ConfigurationError):
            migrations.run_migrations(context, mock.Mock())

    def test_online_mode_requires_url(self) -> None:
        context = mock.Mock()
        context.is_offline_mode.return_value = False
        context.config.attributes = {}

        with self.assertRaises(ConfigurationError):
            migrations.run_migrations(context, mock.Mock())


if __name__ == "__main__":
    unittest.main()
