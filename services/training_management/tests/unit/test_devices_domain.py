"""Reglas de dominio de HU037 (ADR-044): vínculo del dispositivo y muestras del lote."""

import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from device_fakes import device_hash, sample
from fakes import NOW

from training_management.domain.devices.model import (
    AthleteDevice,
    DeviceData,
    DeviceInput,
    DeviceProtocol,
    DeviceStatus,
    DeviceType,
    InvalidDevice,
    validate_device,
    validate_device_id,
)
from training_management.domain.measurements.model import (
    InvalidMeasurementBatch,
    Measurement,
    SampleIssue,
    check_sample,
    check_samples,
    first_occurrences,
    validate_batch,
)

DEVICE_ID = device_hash("AA:BB:CC:DD:EE:FF", uuid4())
STARTED = NOW - timedelta(minutes=10)


def raw(**overrides: Any) -> DeviceInput:
    values: dict[str, Any] = {
        "device_id": DEVICE_ID,
        "display_name": "  Banda pecho  ",
        "device_type": "HEART_RATE_MONITOR",
        "protocol": "BLE_HRS",
    }
    values.update(overrides)
    return DeviceInput(**values)


def at(delta: timedelta) -> str:
    return (NOW + delta).isoformat().replace("+00:00", "Z")


class DeviceValidationTest(unittest.TestCase):
    def test_valid_registration_is_normalized(self) -> None:
        data = validate_device(raw())
        self.assertEqual(data.display_name, "Banda pecho")
        self.assertIs(data.device_type, DeviceType.HEART_RATE_MONITOR)
        self.assertIs(data.protocol, DeviceProtocol.BLE_HRS)

    def test_device_id_is_a_lowercase_sha256_never_a_ble_address(self) -> None:
        self.assertEqual(len(DEVICE_ID), 64)
        for value in ("AA:BB:CC:DD:EE:FF", DEVICE_ID.upper(), DEVICE_ID[:63], DEVICE_ID + "0"):
            with self.subTest(value=value), self.assertRaises(InvalidDevice) as raised:
                validate_device(raw(device_id=value))
            self.assertEqual(raised.exception.violations, (("device_id", "invalid_format"),))

    def test_display_name_length_after_trimming(self) -> None:
        self.assertEqual(validate_device(raw(display_name="x" * 60)).display_name, "x" * 60)
        for value in ("   ", "x" * 61):
            with self.subTest(value=value), self.assertRaises(InvalidDevice) as raised:
                validate_device(raw(display_name=value))
            self.assertEqual(raised.exception.violations, (("display_name", "invalid_length"),))

    def test_only_heart_rate_monitors_over_ble_hrs(self) -> None:
        with self.assertRaises(InvalidDevice) as raised:
            validate_device(raw(device_type="POWER_METER", protocol="ANT_PLUS"))
        self.assertEqual(
            raised.exception.violations,
            (("device_type", "unknown_value"), ("protocol", "unknown_value")),
        )

    def test_missing_fields_report_transport_violations(self) -> None:
        with self.assertRaises(InvalidDevice):
            validate_device(DeviceInput(None, None, None, None))

    def test_path_device_id_is_validated(self) -> None:
        self.assertEqual(validate_device_id(DEVICE_ID), DEVICE_ID)
        with self.assertRaises(InvalidDevice):
            validate_device_id("abc")


class DeviceLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.data = DeviceData(
            DEVICE_ID, "Banda", DeviceType.HEART_RATE_MONITOR, DeviceProtocol.BLE_HRS
        )
        self.device = AthleteDevice.register(athlete_id=uuid4(), data=self.data, now=NOW)

    def test_registration_starts_active_in_version_one(self) -> None:
        self.assertIs(self.device.status, DeviceStatus.ACTIVE)
        self.assertEqual(self.device.version, 1)
        self.assertIsNone(self.device.revoked_at)
        self.assertTrue(self.device.same_registration(self.data))

    def test_revoke_and_reactivate_increment_the_version(self) -> None:
        revoked = self.device.revoke(NOW + timedelta(minutes=1))
        self.assertIs(revoked.status, DeviceStatus.REVOKED)
        self.assertEqual((revoked.version, revoked.revoked_at), (2, NOW + timedelta(minutes=1)))
        with self.assertRaises(ValueError):
            revoked.revoke(NOW)
        renamed = DeviceData(
            DEVICE_ID, "Otra", DeviceType.HEART_RATE_MONITOR, DeviceProtocol.BLE_HRS
        )
        active = revoked.reactivate(renamed, NOW + timedelta(minutes=2))
        self.assertEqual(
            (active.status, active.version, active.display_name), ("ACTIVE", 3, "Otra")
        )
        self.assertIsNone(active.revoked_at)
        with self.assertRaises(ValueError):
            active.reactivate(renamed, NOW)

    def test_another_name_is_not_the_same_registration(self) -> None:
        other = DeviceData(DEVICE_ID, "Otra", DeviceType.HEART_RATE_MONITOR, DeviceProtocol.BLE_HRS)
        self.assertFalse(self.device.same_registration(other))


class SampleValidationTest(unittest.TestCase):
    """CA3: unidad, rango e instante por muestra."""

    def check(self, value: Any) -> Measurement | SampleIssue:
        return check_sample(value, started_at=STARTED, now=NOW)

    def test_valid_sample(self) -> None:
        result = self.check(sample(at(timedelta(seconds=-5)), 20))
        self.assertIsInstance(result, Measurement)
        assert isinstance(result, Measurement)
        self.assertEqual(result.measured_at, NOW - timedelta(seconds=5))
        self.assertIsInstance(self.check(sample(at(timedelta(0)), 250)), Measurement)

    def test_each_rule_has_its_issue(self) -> None:
        cases = {
            "not an object": ("x", SampleIssue.UNKNOWN_VALUE),
            "extra field": ({**sample(at(timedelta(0))), "lat": 1}, SampleIssue.FIELD_NOT_ALLOWED),
            "missing value": (
                {"measured_at": at(timedelta(0)), "type": "HEART_RATE", "unit": "bpm"},
                SampleIssue.REQUIRED,
            ),
            "boolean value": (sample(at(timedelta(0)), True), SampleIssue.UNKNOWN_VALUE),
            "decimal value": (sample(at(timedelta(0)), 120.5), SampleIssue.UNKNOWN_VALUE),
            "other sensor": (sample(at(timedelta(0)), type="POWER"), SampleIssue.UNKNOWN_VALUE),
            "other unit": (sample(at(timedelta(0)), unit="bps"), SampleIssue.INVALID_UNIT),
            "below range": (sample(at(timedelta(0)), 19), SampleIssue.OUT_OF_RANGE),
            "above range": (sample(at(timedelta(0)), 251), SampleIssue.OUT_OF_RANGE),
            "with offset": (
                sample("2026-10-12T11:59:00+00:00"),
                SampleIssue.INVALID_DATETIME,
            ),
            "milliseconds": (sample("2026-10-12T11:59:00.500Z"), SampleIssue.INVALID_DATETIME),
            "impossible date": (sample("2026-02-30T11:59:00Z"), SampleIssue.INVALID_DATETIME),
            "future": (sample(at(timedelta(seconds=6))), SampleIssue.TOO_FAR_IN_FUTURE),
            "older than 15 min": (
                sample(at(-timedelta(minutes=15, seconds=1))),
                SampleIssue.TOO_OLD,
            ),
            "before session": (
                sample(at(-timedelta(minutes=10, seconds=1))),
                SampleIssue.OUT_OF_RANGE,
            ),
        }
        for name, (value, issue) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.check(value), issue)

    def test_limits_are_inclusive(self) -> None:
        self.assertIsInstance(self.check(sample(at(timedelta(seconds=5)))), Measurement)
        self.assertIsInstance(self.check(sample(at(-timedelta(minutes=10)))), Measurement)
        late = check_sample(
            sample(at(-timedelta(minutes=15))),
            started_at=NOW - timedelta(hours=1),
            now=NOW,
        )
        self.assertIsInstance(late, Measurement)

    def test_invalid_samples_do_not_reject_the_batch(self) -> None:
        result = check_samples(
            [sample(at(-timedelta(seconds=2))), sample(at(timedelta(0)), 300), "x"],
            started_at=STARTED,
            now=NOW,
        )
        self.assertEqual([index for index, _ in result.valid], [0])
        self.assertEqual(
            [(item.index, item.issue) for item in result.rejected],
            [(1, SampleIssue.OUT_OF_RANGE), (2, SampleIssue.UNKNOWN_VALUE)],
        )

    def test_repeated_instants_in_a_batch_keep_the_first(self) -> None:
        result = check_samples(
            [
                sample(at(-timedelta(seconds=1)), 100),
                sample(at(-timedelta(seconds=3)), 110),
                sample(at(-timedelta(seconds=1)), 130),
            ],
            started_at=STARTED,
            now=NOW,
        )
        unique = first_occurrences(result.valid)
        self.assertEqual([item.value for item in unique], [100, 110])


class BatchValidationTest(unittest.TestCase):
    def test_valid_batch(self) -> None:
        session_id = uuid4()
        batch = validate_batch(
            str(session_id), {"device_id": DEVICE_ID, "samples": [sample(at(timedelta(0)))]}
        )
        self.assertEqual(
            (batch.session_id, batch.device_id, len(batch.samples)), (session_id, DEVICE_ID, 1)
        )

    def test_batch_shape_violations(self) -> None:
        cases: dict[str, tuple[str, Any, tuple[tuple[str | None, str], ...]]] = {
            "no body": (str(uuid4()), None, ((None, "required"),)),
            "list body": ("x", [], (("session_id", "invalid_uuid"), (None, "unknown_value"))),
            "empty": (
                str(uuid4()),
                {},
                (("device_id", "required"), ("samples", "required")),
            ),
            "wrong types": (
                str(uuid4()),
                {"device_id": 7, "samples": {}, "athlete_id": "x"},
                (
                    ("athlete_id", "field_not_allowed"),
                    ("device_id", "unknown_value"),
                    ("samples", "unknown_value"),
                ),
            ),
            "too many and bad id": (
                str(uuid4()),
                {"device_id": "AA:BB", "samples": [sample(at(timedelta(0)))] * 61},
                (("device_id", "invalid_format"), ("samples", "too_many_items")),
            ),
            "empty samples": (
                str(uuid4()),
                {"device_id": DEVICE_ID, "samples": []},
                (("samples", "required"),),
            ),
        }
        for name, (session_id, body, expected) in cases.items():
            with self.subTest(name), self.assertRaises(InvalidMeasurementBatch) as raised:
                validate_batch(session_id, body)
            self.assertEqual(raised.exception.violations, expected)

    def test_sixty_samples_is_the_maximum(self) -> None:
        samples = [
            sample((datetime(2026, 10, 12, 11, 59, tzinfo=UTC) + timedelta(seconds=i)).isoformat())
            for i in range(60)
        ]
        self.assertEqual(
            len(validate_batch(str(uuid4()), {"device_id": DEVICE_ID, "samples": samples}).samples),
            60,
        )


if __name__ == "__main__":
    unittest.main()
