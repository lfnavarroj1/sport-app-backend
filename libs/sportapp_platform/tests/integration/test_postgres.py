import unittest

from pydantic import SecretStr
from sqlalchemy import create_engine, pool, text

from sportapp_platform.config import DatabasePoolSettings
from sportapp_platform.database import DatabaseReadinessCheck, create_database_engine
from sportapp_platform.testing.postgres import temporary_database


class PostgresIntegrationTest(unittest.TestCase):
    def test_readiness_succeeds_against_real_postgres(self) -> None:
        with temporary_database("platform") as url:
            engine = create_database_engine(SecretStr(url), DatabasePoolSettings())
            try:
                self.assertTrue(DatabaseReadinessCheck("database", engine)())
            finally:
                engine.dispose()

    def test_temporary_database_is_isolated_and_empty(self) -> None:
        with temporary_database("platform") as url:
            engine = create_engine(url, poolclass=pool.NullPool)
            try:
                with engine.connect() as connection:
                    tables = connection.execute(
                        text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
                    ).scalar_one()
            finally:
                engine.dispose()

        self.assertEqual(tables, 0)


if __name__ == "__main__":
    unittest.main()
