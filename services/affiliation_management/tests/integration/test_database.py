"""Migraciones y catálogo de ADR-024 y ADR-036 contra PostgreSQL real."""

import importlib
import unittest
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, delete, func, insert, pool, select

from affiliation_management.domain.catalog import CAPABILITIES_BY_PLAN
from affiliation_management.infrastructure.persistence.catalog import CatalogReadinessCheck
from affiliation_management.infrastructure.persistence.models import (
    Base,
    CapabilityRow,
    CatalogMetadataRow,
    PlanOfferRow,
    PlanRow,
    PlanVersionCapabilityRow,
    PlanVersionRow,
    SubscriptionRow,
)
from affiliation_management.main import create_app
from affiliation_management.settings import AffiliationManagementSettings
from sportapp_platform.migrations import current_revision, downgrade, upgrade
from sportapp_platform.testing.postgres import temporary_database

HISTORY = "affiliation_management.infrastructure.persistence:migrations"
HEAD = "0003_plan_price_offers"
CATALOG = "plans-2026-10-13.v1"
PREVIOUS_CATALOG = "plans-2026-10-11.v1"
seed_0002 = importlib.import_module(
    "affiliation_management.infrastructure.persistence.migrations.versions.0002_free_plan_catalog"
)
seed_0003 = importlib.import_module(
    "affiliation_management.infrastructure.persistence.migrations.versions.0003_plan_price_offers"
)
# Ofertas ratificadas en ADR-036 (precio de referencia en COP antes de impuestos).
EXPECTED_OFFERS = {
    ("FREE", "NONE"): (Decimal("0.00"), None),
    ("INTERMEDIATE", "MONTHLY"): (Decimal("2400.00"), 1),
    ("INTERMEDIATE", "QUARTERLY"): (Decimal("7200.00"), 3),
    ("INTERMEDIATE", "ANNUAL"): (Decimal("28800.00"), 12),
    ("PREMIUM", "MONTHLY"): (Decimal("4800.00"), 1),
    ("PREMIUM", "QUARTERLY"): (Decimal("14400.00"), 3),
    ("PREMIUM", "ANNUAL"): (Decimal("57600.00"), 12),
}


@contextmanager
def migrated_engine(prefix: str, revision: str = "head") -> Iterator[Engine]:
    with temporary_database(prefix) as url:
        upgrade(HISTORY, url, revision)
        engine = create_engine(url, poolclass=pool.NullPool)
        try:
            yield engine
        finally:
            engine.dispose()


def counts(engine: Engine) -> list[int]:
    models = (
        PlanRow,
        PlanVersionRow,
        CapabilityRow,
        PlanVersionCapabilityRow,
        PlanOfferRow,
        CatalogMetadataRow,
    )
    with engine.connect() as connection:
        return [connection.scalar(select(func.count()).select_from(model)) or 0 for model in models]


def capabilities_of(engine: Engine, version: int) -> dict[str, set[str]]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                PlanVersionCapabilityRow.plan_code, PlanVersionCapabilityRow.capability_code
            ).where(PlanVersionCapabilityRow.plan_version == version)
        ).all()
    found: dict[str, set[str]] = {}
    for plan_code, code in rows:
        found.setdefault(plan_code, set()).add(code)
    return found


class MigrationTest(unittest.TestCase):
    def test_migrates_from_empty_database_to_head(self) -> None:
        with temporary_database("affiliation_head") as url:
            upgrade(HISTORY, url)

            self.assertEqual(current_revision(url), HEAD)

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

    def test_upgrade_from_0002_keeps_existing_free_subscriptions(self) -> None:
        """ADR-024: una versión con suscripciones nunca se modifica ni se elimina."""
        with migrated_engine("affiliation_from_0002", "0002_free_plan_catalog") as engine:
            user_id = uuid.uuid4()
            now = datetime.now(UTC)
            with engine.begin() as connection:
                connection.execute(
                    insert(SubscriptionRow).values(
                        subscription_id=uuid.uuid4(),
                        user_id=user_id,
                        plan_code="FREE",
                        plan_version=1,
                        status="ACTIVE",
                        origin="AUTO_REGISTRATION",
                        started_at=now,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
                before = connection.execute(
                    select(PlanVersionRow).where(PlanVersionRow.version == 1)
                ).all()

            upgrade(HISTORY, engine.url.render_as_string(hide_password=False))

            with engine.connect() as connection:
                after = connection.execute(
                    select(PlanVersionRow).where(PlanVersionRow.version == 1)
                ).all()
                subscription = connection.execute(
                    select(SubscriptionRow.plan_code, SubscriptionRow.plan_version).where(
                        SubscriptionRow.user_id == user_id
                    )
                ).one()
            self.assertEqual(after, before, "las versiones 1 quedan intactas")
            self.assertEqual(tuple(subscription), ("FREE", 1))
            self.assertTrue(CatalogReadinessCheck(engine, CATALOG)())

    def test_downgrade_of_0003_restores_the_previous_catalog(self) -> None:
        with temporary_database("affiliation_down_0003") as url:
            upgrade(HISTORY, url)

            downgrade(HISTORY, url, "0002_free_plan_catalog")

            engine = create_engine(url, poolclass=pool.NullPool)
            try:
                with engine.connect() as connection:
                    installed = connection.scalars(select(CatalogMetadataRow.catalog_version)).all()
                    paid = connection.execute(
                        select(PlanRow.status, PlanRow.is_selectable).where(
                            PlanRow.code.in_(("INTERMEDIATE", "PREMIUM"))
                        )
                    ).all()
                self.assertEqual(installed, [PREVIOUS_CATALOG])
                self.assertEqual(set(paid), {("DEFINED", False)})
                self.assertEqual(counts_without_offers(engine), [3, 3, 16, 41, 1])
            finally:
                engine.dispose()


def counts_without_offers(engine: Engine) -> list[int]:
    with engine.connect() as connection:
        return [
            connection.scalar(select(func.count()).select_from(model)) or 0
            for model in (
                PlanRow,
                PlanVersionRow,
                CapabilityRow,
                PlanVersionCapabilityRow,
                CatalogMetadataRow,
            )
        ]


class CatalogSeedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = self.enterContext(migrated_engine("affiliation_seed"))

    def test_seed_installs_the_approved_catalog(self) -> None:
        """HU001 / ADR-036: los tres planes completos, con versiones, ofertas y capacidades."""
        with self.engine.connect() as connection:
            plans = {row.code: row for row in connection.execute(select(PlanRow))}
            versions = {
                (row.plan_code, row.version): row
                for row in connection.execute(select(PlanVersionRow))
            }
            metadata = {
                row.catalog_version: row.migration_revision
                for row in connection.execute(select(CatalogMetadataRow))
            }
            capabilities = connection.scalar(select(func.count()).select_from(CapabilityRow))
            offers = {
                (row.plan_code, row.periodicity): row
                for row in connection.execute(select(PlanOfferRow))
            }

        self.assertEqual(
            metadata,
            {PREVIOUS_CATALOG: "0002_free_plan_catalog", CATALOG: "0003_plan_price_offers"},
        )
        self.assertEqual(capabilities, 16)
        self.assertEqual(
            [(plans[code].name, plans[code].level, plans[code].display_order) for code in plans],
            [("Gratuito", 1, 1), ("Intermedio", 2, 2), ("Premium", 3, 3)],
        )
        self.assertTrue(all(plan.description for plan in plans.values()))
        self.assertTrue(all(plan.status == "ACTIVE" for plan in plans.values()))
        self.assertTrue(all(plan.is_selectable for plan in plans.values()))
        self.assertEqual(
            [code for code, plan in plans.items() if plan.is_default_for_athlete], ["FREE"]
        )
        # Versión 1 intacta (catálogo anterior) y versión 2 en el catálogo vigente.
        for code in plans:
            self.assertEqual(versions[(code, 1)].catalog_version, PREVIOUS_CATALOG)
            current = versions[(code, 2)]
            self.assertEqual(current.catalog_version, CATALOG)
            self.assertEqual(current.currency, "COP")
            self.assertIsNone(current.valid_to)
            self.assertTrue(current.terms)
        self.assertIsNone(versions[("INTERMEDIATE", 1)].price, "la v1 no se reescribe")
        self.assertEqual(versions[("FREE", 2)].price, 0)
        self.assertEqual(versions[("FREE", 2)].periodicity, "NONE")
        self.assertEqual(
            {key: (row.price, row.duration_months) for key, row in offers.items()},
            EXPECTED_OFFERS,
        )
        self.assertTrue(all(row.currency == "COP" for row in offers.values()))
        self.assertTrue(all(row.plan_version == 2 for row in offers.values()))
        expected = {code: set(codes) for code, codes in CAPABILITIES_BY_PLAN.items()}
        self.assertEqual(capabilities_of(self.engine, 1), expected)
        self.assertEqual(capabilities_of(self.engine, 2), expected)

    def test_seeds_can_run_again_without_duplicates(self) -> None:
        before = counts(self.engine)

        with self.engine.begin() as connection:
            seed_0002.seed_catalog(connection)
            seed_0003.seed_catalog(connection)

        self.assertEqual(before, [3, 6, 16, 82, 7, 2])
        self.assertEqual(counts(self.engine), before)

    def test_offer_identifiers_are_deterministic(self) -> None:
        with self.engine.connect() as connection:
            stored = {
                (row.plan_code, row.periodicity): row.offer_id
                for row in connection.execute(select(PlanOfferRow))
            }

        self.assertEqual(stored, {key: seed_0003.offer_id(*key) for key in EXPECTED_OFFERS})


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

    def test_previous_catalog_is_no_longer_ready(self) -> None:
        """Un despliegue que sigue esperando `plans-2026-10-11.v1` no queda listo: sus
        versiones no tienen ofertas (ADR-036 exige actualizar la variable en el mismo release)."""
        with self.assertLogs("sportapp.catalog", level="WARNING") as logs:
            ready = CatalogReadinessCheck(self.engine, PREVIOUS_CATALOG)()

        self.assertFalse(ready)
        self.assertEqual(
            logs.records[0].fields["problem"],  # type: ignore[attr-defined]
            "plan_offers_mismatch:FREE",
        )

    def test_not_ready_when_a_plan_capability_is_missing(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                delete(PlanVersionCapabilityRow).where(
                    PlanVersionCapabilityRow.plan_code == "PREMIUM",
                    PlanVersionCapabilityRow.plan_version == 2,
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

    def test_not_ready_when_an_offer_is_missing(self) -> None:
        """ADR-036: la sonda falla si las ofertas no están completas."""
        with self.engine.begin() as connection:
            connection.execute(
                delete(PlanOfferRow).where(
                    PlanOfferRow.plan_code == "INTERMEDIATE", PlanOfferRow.periodicity == "ANNUAL"
                )
            )

        with self.assertLogs("sportapp.catalog", level="WARNING") as logs:
            ready = CatalogReadinessCheck(self.engine, CATALOG)()

        self.assertFalse(ready)
        self.assertEqual(
            logs.records[0].fields["problem"],  # type: ignore[attr-defined]
            "plan_offers_mismatch:INTERMEDIATE",
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
