"""HU037 contra PostgreSQL real: vínculo, revocación que corta la ingestión (CA6), duplicados y
desorden (CA4), lotes cada 5 s (CA5), carreras y restricciones de la base."""

import hashlib
import threading
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from support import (
    TrainingDatabases,
    athlete_and_professional,
    finish_command,
    start_command,
    training_databases,
)

from training_management.application.commands.devices import (
    RegisterDevice,
    RegisterDeviceCommand,
    RevokeDevice,
)
from training_management.application.commands.measurements import (
    IngestMeasurements,
    IngestMeasurementsCommand,
)
from training_management.application.errors import TrainingSessionAlreadyFinished
from training_management.application.monitoring_threshold import ResolveThreshold
from training_management.application.queries.devices import ListMyDevices
from training_management.domain.devices.model import (
    DeviceAlreadyRegistered,
    DeviceInput,
    DeviceNotFound,
    DeviceRevoked,
)
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.persistence_write.models import (
    AthleteDeviceRow,
    TrainingMeasurementRow,
)
from training_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyDeviceReader,
    SqlAlchemyDeviceUnitOfWork,
    SqlAlchemyMeasurementUnitOfWork,
    SqlAlchemyMonitoringSettingsUnitOfWork,
)


class AlwaysCapable:
    def has_capability(self, user_id: Any, capability: str) -> bool:
        return True


class NoBirthDate:
    """Contexto de Usuarios sin perfil demográfico: umbral por defecto 171 (HU036)."""

    def birth_date(self, athlete_id: Any) -> None:
        return None


def device_hash(address: str, user_id: Any) -> str:
    return hashlib.sha256(f"{address}{user_id}".encode()).hexdigest()


def instant(seconds_ago: int) -> str:
    moment = (datetime.now(UTC) - timedelta(seconds=seconds_ago)).replace(microsecond=0)
    return moment.isoformat().replace("+00:00", "Z")


def samples(*seconds_ago: int, value: int = 140) -> list[dict[str, Any]]:
    return [
        {"measured_at": instant(ago), "type": "HEART_RATE", "value": value, "unit": "bpm"}
        for ago in seconds_ago
    ]


def concurrently(count: int, action: Any) -> tuple[list[Any], list[Exception]]:
    barrier = threading.Barrier(count)
    results: list[Any] = []
    errors: list[Exception] = []

    def run() -> None:
        barrier.wait()
        try:
            results.append(action())
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results, errors


class DevicePersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: TrainingDatabases = self.enterContext(training_databases("hu037"))
        self.athlete, _ = athlete_and_professional()
        self.device_id = device_hash("AA:BB:CC:DD:EE:FF", self.athlete.user_id)
        clock = SystemClock()
        self.register = RegisterDevice(
            unit_of_work=lambda: SqlAlchemyDeviceUnitOfWork(self.db.writes),
            capabilities=AlwaysCapable(),
            clock=clock,
        )
        self.revoke = RevokeDevice(
            unit_of_work=lambda: SqlAlchemyDeviceUnitOfWork(self.db.writes),
            capabilities=AlwaysCapable(),
            clock=clock,
        )
        self.list_devices = ListMyDevices(
            reader=SqlAlchemyDeviceReader(self.db.writes), capabilities=AlwaysCapable()
        )
        self.ingest = IngestMeasurements(
            unit_of_work=lambda: SqlAlchemyMeasurementUnitOfWork(self.db.writes),
            capabilities=AlwaysCapable(),
            thresholds=ResolveThreshold(
                unit_of_work=lambda: SqlAlchemyMonitoringSettingsUnitOfWork(self.db.writes),
                birth_dates=NoBirthDate(),
                clock=clock,
            ),
            clock=clock,
        )

    def link(self, name: str = "Banda pecho", device_id: str | None = None, key: Any = None) -> Any:
        return self.register(
            self.athlete,
            RegisterDeviceCommand(
                device=DeviceInput(
                    device_id or self.device_id, name, "HEART_RATE_MONITOR", "BLE_HRS"
                ),
                idempotency_key=key or uuid4(),
            ),
        )

    def started_session(self) -> Any:
        return self.db.start_session()(
            self.athlete, start_command(started_at=instant(20 * 60))
        ).session

    def send(
        self, session_id: Any, batch: list[dict[str, Any]], device_id: str | None = None
    ) -> Any:
        return self.ingest(
            self.athlete,
            IngestMeasurementsCommand(
                session_id=str(session_id),
                body={"device_id": device_id or self.device_id, "samples": batch},
            ),
        )

    def stored(self) -> list[tuple[datetime, int]]:
        with self.db.writes() as session:
            rows = session.execute(
                select(TrainingMeasurementRow.measured_at, TrainingMeasurementRow.value).order_by(
                    TrainingMeasurementRow.measured_at
                )
            ).all()
        return [(row[0], row[1]) for row in rows]

    # ------------------------------------------------------------ vínculo (CA1)

    def test_link_list_revoke_and_link_again(self) -> None:
        key = uuid4()
        first = self.link(key=key)
        self.assertEqual((first.device.status, first.device.version), ("ACTIVE", 1))
        self.assertTrue(self.link(key=key).replayed)
        self.assertFalse(self.link().changed)
        with self.assertRaises(DeviceAlreadyRegistered):
            self.link("Otro nombre")
        revoked = self.revoke(self.athlete, self.device_id)
        self.assertEqual((revoked.device.status, revoked.device.version), ("REVOKED", 2))
        self.assertIsNotNone(revoked.device.revoked_at)
        self.assertFalse(self.revoke(self.athlete, self.device_id).changed)
        again = self.link("Banda nueva")
        self.assertEqual((again.device.status, again.device.version), ("ACTIVE", 3))
        listed = self.list_devices(self.athlete)
        self.assertEqual(
            [(item.display_name, item.status) for item in listed], [("Banda nueva", "ACTIVE")]
        )
        self.assertEqual(self.db.count(AthleteDeviceRow), 1)
        with self.assertRaises(DeviceNotFound):
            self.revoke(athlete_and_professional()[0], self.device_id)

    def test_concurrent_links_create_a_single_device(self) -> None:
        results, errors = concurrently(5, self.link)
        self.assertEqual(errors, [])
        self.assertEqual(sum(result.changed for result in results), 1)
        self.assertEqual(self.db.count(AthleteDeviceRow), 1)

    def test_concurrent_retries_with_the_same_key_replay_the_first_result(self) -> None:
        key = uuid4()
        results, errors = concurrently(5, lambda: self.link(key=key))
        self.assertEqual(errors, [])
        self.assertEqual(sum(not result.replayed for result in results), 1)
        self.assertTrue(all(result.device.version == 1 for result in results))
        self.assertEqual(self.db.count(AthleteDeviceRow), 1)

    def test_database_never_stores_a_ble_address(self) -> None:
        with self.assertRaises(IntegrityError), self.db.writes() as session, session.begin():
            session.execute(
                text(
                    "INSERT INTO athlete_device VALUES (:athlete, 'AA:BB:CC:DD:EE:FF', 'x', "
                    "'HEART_RATE_MONITOR', 'BLE_HRS', 'ACTIVE', 1, now(), now(), NULL)"
                ),
                {"athlete": self.athlete.user_id},
            )

    # ------------------------------------------------------------ ingestión (CA3-CA6)

    def test_batches_every_five_seconds_are_all_recorded(self) -> None:
        """CA5: un minuto a 1 Hz en 12 lotes de 5 s; reenviar uno solo suma duplicados."""
        self.link()
        session = self.started_session()
        for start in range(60, 0, -5):
            result = self.send(session.session_id, samples(*range(start, start - 5, -1)))
            self.assertEqual((result.accepted, result.duplicates, result.rejected), (5, 0, ()))
        resent = self.send(session.session_id, samples(*range(30, 25, -1)))
        self.assertEqual((resent.accepted, resent.duplicates), (0, 5))
        self.assertEqual(len(self.stored()), 60)

    def test_duplicates_and_out_of_order(self) -> None:
        """CA4: trazabilidad por dispositivo e instante; la primera muestra registrada se
        conserva."""
        self.link()
        session = self.started_session()
        first = self.send(session.session_id, samples(3, 1, 2, value=150))
        self.assertEqual(first.accepted, 3)
        mixed = self.send(session.session_id, [*samples(2, value=99), *samples(10, 3, value=160)])
        self.assertEqual((mixed.accepted, mixed.duplicates), (1, 2))
        self.assertEqual([value for _, value in self.stored()], [160, 150, 150, 150])
        other_device = device_hash("11:22:33:44:55:66", self.athlete.user_id)
        self.link("Reloj", other_device)
        same_instants = self.send(session.session_id, samples(3, 2, 1), other_device)
        self.assertEqual(same_instants.accepted, 3)

    def test_concurrent_resends_insert_each_sample_once(self) -> None:
        self.link()
        session = self.started_session()
        batch = samples(5, 4, 3, 2, 1)
        results, errors = concurrently(4, lambda: self.send(session.session_id, batch))
        self.assertEqual(errors, [])
        self.assertEqual(sum(result.accepted for result in results), 5)
        self.assertEqual(sum(result.duplicates for result in results), 15)
        self.assertEqual(len(self.stored()), 5)

    def test_invalid_samples_are_reported_without_rejecting_the_batch(self) -> None:
        """CA3."""
        self.link()
        session = self.started_session()
        batch = [
            *samples(4),
            *samples(3, value=10),
            {**samples(2)[0], "unit": "rpm"},
            *samples(16 * 60),
            *samples(1),
        ]
        result = self.send(session.session_id, batch)
        self.assertEqual(result.accepted, 2)
        self.assertEqual(
            [(item.index, item.issue.value) for item in result.rejected],
            [(1, "out_of_range"), (2, "invalid_unit"), (3, "too_old")],
        )

    def test_revocation_cuts_ingestion(self) -> None:
        """CA6: tras revocar, `device_revoked`; las mediciones previas se conservan."""
        self.link()
        session = self.started_session()
        self.send(session.session_id, samples(2, 1))
        self.revoke(self.athlete, self.device_id)
        with self.assertRaises(DeviceRevoked):
            self.send(session.session_id, samples(0))
        self.assertEqual(len(self.stored()), 2)

    def test_revocation_waits_for_an_in_flight_batch(self) -> None:
        """La revocación (`FOR UPDATE`) espera al lote que tiene el dispositivo en `FOR SHARE`."""
        self.link()
        session = self.started_session()
        with self.db.writes() as holder, holder.begin():
            holder.execute(
                text("SELECT 1 FROM athlete_device WHERE device_id = :device FOR SHARE"),
                {"device": self.device_id},
            )
            revoking = threading.Thread(target=lambda: self.revoke(self.athlete, self.device_id))
            revoking.start()
            revoking.join(timeout=1)
            self.assertTrue(revoking.is_alive())
        revoking.join(timeout=30)
        self.assertFalse(revoking.is_alive())
        with self.assertRaises(DeviceRevoked):
            self.send(session.session_id, samples(0))

    def test_only_own_active_sessions_and_linked_devices(self) -> None:
        self.link()
        session = self.started_session()
        with self.assertRaises(DeviceNotFound):
            self.send(session.session_id, samples(1), device_hash("00", self.athlete.user_id))
        self.send(session.session_id, samples(1))
        self.db.finish_session()(
            self.athlete,
            finish_command(session.session_id, ended_at=instant(0)),
        )
        with self.assertRaises(TrainingSessionAlreadyFinished):
            self.send(session.session_id, samples(0))
        self.assertEqual(len(self.stored()), 1)


if __name__ == "__main__":
    unittest.main()
