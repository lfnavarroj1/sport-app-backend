"""Comandos del perfil (HU005) contra PostgreSQL real: sección, outbox e idempotencia en
una transacción, control optimista bajo concurrencia y migración `0004`."""

import threading
import unittest
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, insert, inspect, pool, select
from sqlalchemy.exc import IntegrityError
from support import HEALTH_CONSENT, WRITE_HISTORY, WriteDatabase, write_database

from sportapp_platform.migrations import current_revision, downgrade, upgrade
from sportapp_platform.testing.postgres import temporary_database
from users_management.api.profile_payload import parse_demographic, parse_physiological
from users_management.application.commands.profile_section import (
    ProfileSectionCommand,
    ProfileSectionResult,
)
from users_management.application.commands.update_physiological_profile import (
    UpdatePhysiologicalProfile,
)
from users_management.application.errors import IdempotencyKeyReused
from users_management.application.ports import OutboxMessage
from users_management.domain.profile.events import ProfileSection
from users_management.domain.profile.section import VersionConflict
from users_management.domain.profile.validation import InvalidProfileSection
from users_management.infrastructure.clock import SystemClock
from users_management.infrastructure.persistence_write.models import (
    DemographicProfileRow,
    IdempotencyKeyRow,
    OutboxEventRow,
    PhysiologicalProfileRow,
)
from users_management.infrastructure.persistence_write.profile import (
    ProfileWriteFailed,
    SqlAlchemyProfileUnitOfWork,
)

CONCURRENT_REQUESTS = 8


def demographic(key: UUID | None = None, **overrides: Any) -> ProfileSectionCommand[Any]:
    body: dict[str, Any] = {
        "birth_date": "1992-04-18",
        "gender": "FEMALE",
        "country_code": "CO",
        "city": "Bogotá",
        "marital_status": "SINGLE",
        "occupation": "Ingeniera",
        "income_range": "2_4_SMMLV",
        **overrides,
    }
    return ProfileSectionCommand(parse_demographic(body), key or uuid4(), uuid4())


def physiological(key: UUID | None = None, **overrides: Any) -> ProfileSectionCommand[Any]:
    measured = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body: dict[str, Any] = {
        "consent": {"version": HEALTH_CONSENT},
        "measured_at": measured,
        "weight_kg": "68.5",
        "height_cm": 172,
        "resting_heart_rate_bpm": 58,
        "blood_pressure": {"systolic_mmhg": 118, "diastolic_mmhg": 76},
        "body_temperature_c": "36.6",
        **overrides,
    }
    return ProfileSectionCommand(parse_physiological(body), key or uuid4(), uuid4())


def run_concurrently(calls: list[Callable[[], Any]]) -> list[Any]:
    """Ejecuta las llamadas a la vez; devuelve el resultado o la excepción de cada una."""
    barrier = threading.Barrier(len(calls))
    outcomes: list[Any] = [None] * len(calls)

    def worker(index: int) -> None:
        barrier.wait()
        try:
            outcomes[index] = calls[index]()
        except Exception as error:  # La prueba clasifica cada resultado.
            outcomes[index] = error

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(len(calls))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return outcomes


class FailingOutboxUnitOfWork(SqlAlchemyProfileUnitOfWork):
    """Fuerza un fallo de PostgreSQL al confirmar: el tipo de evento excede su columna."""

    def add_outbox(self, message: OutboxMessage) -> None:
        super().add_outbox(replace(message, event_type="x" * 200))


class ProfilePersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._context = write_database("users_management_profile")
        self.db: WriteDatabase = self._context.__enter__()
        self.addCleanup(self._context.__exit__, None, None, None)

    def rows(self, model: type[DemographicProfileRow | PhysiologicalProfileRow]) -> list[Any]:
        with self.db.sessions() as session:
            return list(session.scalars(select(model)))

    def events(self) -> list[OutboxEventRow]:
        with self.db.sessions() as session:
            return list(
                session.scalars(select(OutboxEventRow).order_by(OutboxEventRow.occurred_at))
            )

    def test_create_and_replace_demographic_section(self) -> None:
        """CA1 y CA4: alta (`201`) y reemplazo (`200`) con versión, evento y clave."""
        user_id = self.db.athlete()
        update = self.db.update_demographic()

        first = update(user_id, demographic())
        second = update(user_id, demographic(expected_version=1, city="  Cali "))

        self.assertEqual((first.status, second.status), (201, 200))
        [row] = self.rows(DemographicProfileRow)
        self.assertEqual((row.user_id, row.version, row.city), (user_id, 2, "Cali"))
        self.assertEqual(second.document["created_at"], first.document["created_at"])
        registration_events = 1
        profile_events = [e for e in self.events() if e.aggregate_type == "demographic_profile"]
        self.assertEqual(len(self.events()), registration_events + 2)
        self.assertEqual([e.aggregate_version for e in profile_events], [1, 2])
        self.assertEqual(profile_events[-1].payload, second.document)
        self.assertEqual(
            {e.event_type for e in profile_events}, {"perfil.demografico_actualizado.v1"}
        )
        self.assertTrue(all(e.aggregate_id == user_id for e in profile_events))

    def test_physiological_section_round_trips_typed_columns(self) -> None:
        """CA1: `NUMERIC(4,1)`, consentimiento y documento reconstruido idéntico."""
        user_id = self.db.athlete()
        update = self.db.update_physiological()

        created = update(user_id, physiological())

        [row] = self.rows(PhysiologicalProfileRow)
        self.assertEqual((row.weight_kg, row.bmi), (Decimal("68.5"), Decimal("23.2")))
        self.assertEqual(row.body_temperature_c, Decimal("36.6"))
        self.assertEqual((row.systolic_mmhg, row.diastolic_mmhg), (118, 76))
        self.assertEqual(row.health_consent_version, HEALTH_CONSENT)
        with self.db.profile() as unit_of_work:
            stored = unit_of_work.lock_section(ProfileSection.PHYSIOLOGICAL, user_id)
        assert stored is not None
        self.assertEqual(stored.document, created.document)

        without_optionals = update(
            user_id,
            physiological(
                expected_version=1,
                blood_pressure=None,
                resting_heart_rate_bpm=None,
                body_temperature_c=None,
            ),
        )
        with self.db.profile() as unit_of_work:
            stored = unit_of_work.lock_section(ProfileSection.PHYSIOLOGICAL, user_id)
        assert stored is not None
        self.assertEqual(stored.document, without_optionals.document)
        self.assertIsNone(stored.document["blood_pressure"])

    def test_rejections_leave_no_section_event_or_key(self) -> None:
        """CA2, CA3 y CA6: un `422` o un `409` no deja filas ni evento."""
        user_id = self.db.athlete()
        before = (self.db.count(OutboxEventRow), self.db.count(IdempotencyKeyRow))

        with self.assertRaises(InvalidProfileSection):
            self.db.update_physiological()(user_id, physiological(consent={"version": "old"}))
        with self.assertRaises(InvalidProfileSection):
            self.db.update_demographic()(user_id, demographic(bmi="1"))
        with self.assertRaises(VersionConflict):
            self.db.update_demographic()(user_id, demographic(expected_version=1))

        self.assertEqual(self.db.count(DemographicProfileRow), 0)
        self.assertEqual(self.db.count(PhysiologicalProfileRow), 0)
        self.assertEqual((self.db.count(OutboxEventRow), self.db.count(IdempotencyKeyRow)), before)

    def test_section_outbox_and_key_commit_atomically(self) -> None:
        """CA4: si la outbox falla, tampoco quedan la sección ni la clave; sin valores."""
        user_id = self.db.athlete()
        before = self.db.count(IdempotencyKeyRow)
        update = UpdatePhysiologicalProfile(
            unit_of_work=lambda: FailingOutboxUnitOfWork(self.db.sessions),
            clock=SystemClock(),
            current_consent_version=HEALTH_CONSENT,
        )

        with self.assertRaises(ProfileWriteFailed) as caught:
            update(user_id, physiological())

        self.assertEqual(self.db.count(PhysiologicalProfileRow), 0)
        self.assertEqual(self.db.count(IdempotencyKeyRow), before)
        message = str(caught.exception)
        for value in ("68.5", "36.6", "23.2", HEALTH_CONSENT):
            self.assertNotIn(value, message)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_eight_concurrent_replacements_create_one_version(self) -> None:
        """CA6: ocho reemplazos con la misma `expected_version` → una versión y siete 409."""
        user_id = self.db.athlete()
        update = self.db.update_demographic()
        update(user_id, demographic())

        outcomes = run_concurrently(
            [
                lambda index=index: update(
                    user_id, demographic(expected_version=1, city=f"Ciudad {index}")
                )
                for index in range(CONCURRENT_REQUESTS)
            ]
        )

        succeeded = [item for item in outcomes if isinstance(item, ProfileSectionResult)]
        conflicts = [item for item in outcomes if isinstance(item, VersionConflict)]
        self.assertEqual((len(succeeded), len(conflicts)), (1, CONCURRENT_REQUESTS - 1), outcomes)
        self.assertEqual(succeeded[0].document["version"], 2)
        self.assertTrue(all(conflict.current_version == 2 for conflict in conflicts))
        [row] = self.rows(DemographicProfileRow)
        self.assertEqual((row.version, row.city), (2, succeeded[0].document["city"]))
        profile_events = [e for e in self.events() if e.aggregate_type == "demographic_profile"]
        self.assertEqual([e.aggregate_version for e in profile_events], [1, 2])

    def test_concurrent_creations_create_one_section(self) -> None:
        """Altas simultáneas: una crea la versión 1; las demás, `409` con la versión vigente."""
        user_id = self.db.athlete()
        update = self.db.update_physiological()

        outcomes = run_concurrently(
            [lambda: update(user_id, physiological()) for _ in range(CONCURRENT_REQUESTS)]
        )

        succeeded = [item for item in outcomes if isinstance(item, ProfileSectionResult)]
        conflicts = [item for item in outcomes if isinstance(item, VersionConflict)]
        self.assertEqual((len(succeeded), len(conflicts)), (1, CONCURRENT_REQUESTS - 1), outcomes)
        self.assertTrue(all(conflict.current_version == 1 for conflict in conflicts))
        self.assertEqual(self.db.count(PhysiologicalProfileRow), 1)

    def test_concurrent_retries_with_same_key_produce_one_effect(self) -> None:
        """CA6: reintentos simultáneos con la misma clave → mismo resultado, un evento."""
        user_id = self.db.athlete()
        update = self.db.update_demographic()
        key = uuid4()

        outcomes = run_concurrently(
            [lambda: update(user_id, demographic(key)) for _ in range(CONCURRENT_REQUESTS)]
        )

        self.assertTrue(all(isinstance(item, ProfileSectionResult) for item in outcomes), outcomes)
        self.assertEqual({item.document["version"] for item in outcomes}, {1})
        self.assertEqual({item.status for item in outcomes}, {201})
        self.assertEqual(sum(not item.replayed for item in outcomes), 1)
        profile_events = [e for e in self.events() if e.aggregate_type == "demographic_profile"]
        self.assertEqual(len(profile_events), 1)

    def test_athletes_are_isolated(self) -> None:
        """CA1 y CA5: cada sección pertenece a un solo deportista; la clave no se comparte."""
        first, second = self.db.athlete(), self.db.athlete()
        update = self.db.update_demographic()
        key = uuid4()

        update(first, demographic(key))
        update(second, demographic())
        with self.assertRaises(IdempotencyKeyReused):
            update(second, demographic(key))

        rows = {row.user_id: row.version for row in self.rows(DemographicProfileRow)}
        self.assertEqual(rows, {first: 1, second: 1})

    def test_database_rejects_out_of_contract_values(self) -> None:
        """Las restricciones de la tabla son equivalentes al contrato (última defensa)."""
        user_id = self.db.athlete()
        now = datetime.now(UTC)
        valid: dict[str, Any] = {
            "user_id": user_id,
            "version": 1,
            "health_consent_version": HEALTH_CONSENT,
            "health_consent_at": now,
            "measured_at": now,
            "weight_kg": Decimal("68.5"),
            "height_cm": 172,
            "bmi": Decimal("23.2"),
            "created_at": now,
            "updated_at": now,
        }
        for overrides in (
            {"weight_kg": Decimal("19.9")},
            {"height_cm": 251},
            {"systolic_mmhg": 80, "diastolic_mmhg": 90},
            {"systolic_mmhg": 120},
            {"body_temperature_c": Decimal("42.1")},
            {"version": 0},
        ):
            with self.subTest(overrides=list(overrides)), self.db.engine.connect() as connection:
                with self.assertRaises(IntegrityError):
                    connection.execute(
                        insert(PhysiologicalProfileRow).values(**{**valid, **overrides})
                    )


class AthleteProfileMigrationTest(unittest.TestCase):
    def test_downgrade_removes_profile_tables_and_upgrade_restores_them(self) -> None:
        """`0004` es reversible: quita solo sus tablas y se puede volver a aplicar."""
        with temporary_database("users_management_0004") as url:
            upgrade(WRITE_HISTORY, url)

            downgrade(WRITE_HISTORY, url, "0003_authenticate_user")

            engine = create_engine(url, poolclass=pool.NullPool)
            try:
                tables = set(inspect(engine).get_table_names())
            finally:
                engine.dispose()
            self.assertEqual(current_revision(url), "0003_authenticate_user")
            self.assertNotIn("demographic_profile", tables)
            self.assertNotIn("physiological_profile", tables)
            self.assertIn("user_account", tables)

            upgrade(WRITE_HISTORY, url)

            self.assertEqual(current_revision(url), "0006_nutrition_habits")


if __name__ == "__main__":
    unittest.main()
