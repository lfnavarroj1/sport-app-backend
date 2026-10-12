import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pydantic import SecretStr

from sportapp_platform.config import ConfigurationError, DatabasePoolSettings
from sportapp_platform.database import DatabaseReadinessCheck, create_database_engine, new_metadata
from sportapp_platform.testing.architecture import find_layer_violations
from sportapp_platform.testing.postgres import admin_database_url

UNREACHABLE_URL = SecretStr("postgresql+psycopg://probe@127.0.0.1:1/probe")


class LayerViolationsTest(unittest.TestCase):
    def _package(self, files: dict[str, str]) -> Path:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "probe_service"
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return root

    def test_accepts_pure_domain_and_application_using_domain(self) -> None:
        package = self._package(
            {
                "domain/model.py": "from dataclasses import dataclass\n",
                "application/use_case.py": "from probe_service.domain.model import dataclass\n",
                "infrastructure/db.py": "import sqlalchemy\n",
            }
        )

        self.assertEqual(find_layer_violations(package), [])

    def test_reports_framework_and_inner_layer_violations(self) -> None:
        package = self._package(
            {
                "domain/model.py": "import sqlalchemy\nfrom probe_service.api import x\n",
                "application/use_case.py": "from fastapi import APIRouter\n",
            }
        )

        violations = find_layer_violations(package)

        self.assertEqual(
            violations,
            [
                str(Path("domain/model.py")) + " -> sqlalchemy",
                str(Path("domain/model.py")) + " -> probe_service.api",
                str(Path("application/use_case.py")) + " -> fastapi",
            ],
        )


class DatabaseTest(unittest.TestCase):
    def test_metadata_uses_naming_convention(self) -> None:
        self.assertIn("pk", new_metadata().naming_convention)

    def test_engine_applies_pool_limits(self) -> None:
        pool_settings = DatabasePoolSettings(db_pool_size=3, db_max_overflow=1)

        engine = create_database_engine(UNREACHABLE_URL, pool_settings)

        self.assertEqual(engine.pool.size(), 3)  # type: ignore[attr-defined]
        engine.dispose()

    def test_readiness_fails_when_database_is_unreachable(self) -> None:
        engine = create_database_engine(
            UNREACHABLE_URL, DatabasePoolSettings(db_connect_timeout_seconds=1)
        )
        check = DatabaseReadinessCheck("database", engine)

        with self.assertLogs("sportapp.database", level="WARNING"):
            self.assertFalse(check())
        self.assertEqual(check.name, "database")
        engine.dispose()


class AdminDatabaseUrlTest(unittest.TestCase):
    def test_missing_test_database_url_fails_explicitly(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigurationError):
                admin_database_url()


if __name__ == "__main__":
    unittest.main()
