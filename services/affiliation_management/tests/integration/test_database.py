"""Migración y catálogo de ADR-024 contra PostgreSQL real."""

import importlib
import unittest
from collections.abc import Iterator
from contextlib import contextmanager

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, delete, func, pool, select

from affiliation_management.domain.catalog import CAPABILITIES_BY_PLAN
from affiliation_management.infrastructure.persistence.catalog import CatalogReadinessCheck
from affiliation_management.infrastructure.persistence.models import (
    Base,
    CapabilityRow,
    CatalogMetadataRow,
    PlanRow,
    PlanVersionCapabilityRow,
    PlanVersionRow,
)
from affiliation_management.main import create_app
from affiliation_management.settings import AffiliationManagementSettings
from sportapp_platform.migrations import current_revision, downgrade, upgrade
from sportapp_platform.testing.postgres import temporary_database

HISTORY = "affiliation_management.infrastructure.persistence:migrations"
CATALOG = "plans-2026-10-11.v1"
seed_module = importlib.import_module(
    "affiliation_management.infrastructure.persistence.migrations.versions.0002_free_plan_catalog"
)


@contextmanager
def migrated_engine(prefix: str) -> Iterator[Engine]:
    with temporary_database(prefix) as url:
        upgrade(HISTORY, url)
        engine = create_engine(url, poolclass=pool.NullPool)
        try:
            yield engine
        finally:
            engine.dispose()


class MigrationTest(unittest.TestCase):
    def test_migrates_from_empty_database_to_head(self) -> None:
        with temporary_database("affiliation_head") as url:
            upgrade(HISTORY, url)

            self.assertEqual(current_revision(url), "0002_free_plan_catalog")

    def test_migration_matches_orm_models(self) -> None:
        """Crear el ORM no equivale a una migración válida: ambos deben coincidir."""
        with migrated_engine("affiliation_drift") as engine, engine.connect() as connection:
            differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)

        self.assertEqual(differences, [])

    def test_downgrades_to_baseline(self) -> None:
        with temporary_database("affiliation_downgrade") as url:
            upgrade(HISTORY, url)

            downgrade(HISTORY, url, "0001_baseline")

            self.assertEqual(current_revision(url), "0001_baseline")


class CatalogSeedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = self.enterContext(migrated_engine("affiliation_seed"))

    def test_seed_installs_the_approved_catalog(self) -> None:
        with self.engine.connect() as connection:
            plans = {row.code: row for row in connection.execute(select(PlanRow))}
            versions = {row.plan_code: row for row in connection.execute(select(PlanVersionRow))}
            capabilities = connection.scalar(select(func.count()).select_from(CapabilityRow))
            installed = connection.scalars(select(CatalogMetadataRow.catalog_version)).all()
            per_plan = {
                code: set(
                    connection.scalars(
                        select(PlanVersionCapabilityRow.capability_code).where(
                            PlanVersionCapabilityRow.plan_code == code
                        )
                    )
                )
                for code in plans
            }

        self.assertEqual(installed, [CATALOG])
        self.assertEqual(capabilities, 16)
        self.assertEqual(set(plans), {"FREE", "INTERMEDIATE", "PREMIUM"})
        free = plans["FREE"]
        self.assertEqual((free.name, free.level, free.status), ("Gratuito", 1, "ACTIVE"))
        self.assertTrue(free.is_default_for_athlete)
        self.assertTrue(free.is_selectable)
        for paid in ("INTERMEDIATE", "PREMIUM"):
            self.assertFalse(plans[paid].is_selectable, "sin datos comerciales no es seleccionable")
            self.assertFalse(plans[paid].is_default_for_athlete)
            self.assertIsNone(versions[paid].price, "precio pendiente: no se inventa")
            self.assertIsNone(versions[paid].periodicity)
        self.assertEqual(versions["FREE"].price, 0)
        self.assertEqual(versions["FREE"].periodicity, "NONE")
        self.assertIsNone(versions["FREE"].currency)
        self.assertTrue(all(row.catalog_version == CATALOG for row in versions.values()))
        self.assertEqual(
            {code: len(codes) for code, codes in per_plan.items()},
            {"FREE": 11, "INTERMEDIATE": 14, "PREMIUM": 16},
        )
        self.assertEqual(
            per_plan, {code: set(codes) for code, codes in CAPABILITIES_BY_PLAN.items()}
        )

    def test_seed_can_run_again_without_duplicates(self) -> None:
        with self.engine.begin() as connection:
            seed_module.seed_catalog(connection)

        with self.engine.connect() as connection:
            counts = [
                connection.scalar(select(func.count()).select_from(model))
                for model in (PlanRow, PlanVersionRow, CapabilityRow, PlanVersionCapabilityRow)
            ]
        self.assertEqual(counts, [3, 3, 16, 41])


class CatalogReadinessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = self.enterContext(migrated_engine("affiliation_ready"))

    def test_ready_with_the_expected_complete_catalog(self) -> None:
        self.assertTrue(CatalogReadinessCheck(self.engine, CATALOG)())

    def test_not_ready_when_expected_version_differs(self) -> None:
        with self.assertLogs("sportapp.catalog", level="WARNING") as logs:
            ready = CatalogReadinessCheck(self.engine, "plans-2099-01-01.v9")()

        self.assertFalse(ready)
        self.assertEqual(logs.records[0].fields["problem"], "catalog_version_missing")  # type: ignore[attr-defined]

    def test_not_ready_when_a_plan_capability_is_missing(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                delete(PlanVersionCapabilityRow).where(
                    PlanVersionCapabilityRow.plan_code == "PREMIUM",
                    PlanVersionCapabilityRow.capability_code
                    == "services.virtual_sessions.schedule",
                )
            )

        with self.assertLogs("sportapp.catalog", level="WARNING") as logs:
            ready = CatalogReadinessCheck(self.engine, CATALOG)()

        self.assertFalse(ready)
        self.assertEqual(
            logs.records[0].fields["problem"],  # type: ignore[attr-defined]
            "plan_capabilities_mismatch:PREMIUM",
        )

    def test_not_ready_without_catalog_metadata(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(delete(CatalogMetadataRow))

        with self.assertLogs("sportapp.catalog", level="WARNING"):
            self.assertFalse(CatalogReadinessCheck(self.engine, CATALOG)())

    def test_service_reports_ready_against_real_postgres(self) -> None:
        url = self.engine.url.render_as_string(hide_password=False)
        settings = AffiliationManagementSettings(
            app_env="test",  # type: ignore[arg-type]
            database_url=url,  # type: ignore[arg-type]
            affiliation_expected_catalog_version=CATALOG,
        )
        with TestClient(create_app(settings)) as client:
            response = client.get("/health/ready")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["checks"], {"database": "ok", "catalog": "ok"})


if __name__ == "__main__":
    unittest.main()
