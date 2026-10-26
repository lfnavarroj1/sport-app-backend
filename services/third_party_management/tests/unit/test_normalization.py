"""Normalización `strava-activity.v1` (ADR-042 §10, CA4, CA5 y CA7): deportes, instantes,
zonas, unidades, redondeos, nombre, hash, versiones y actividades parciales."""

import hashlib
import unittest
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

from fakes import NOW

from third_party_management.domain.activities import (
    InvalidActivity,
    SkipReason,
    Sport,
    next_import,
    normalize_activity,
    round_meters,
    time_zone_from,
)
from third_party_management.testing.strava_simulator import activity


class SportMappingTest(unittest.TestCase):
    def test_maps_each_strava_type_of_adr_042(self) -> None:
        expected = {
            "Run": Sport.RUNNING,
            "TrailRun": Sport.RUNNING,
            "VirtualRun": Sport.RUNNING,
            "Ride": Sport.CYCLING,
            "VirtualRide": Sport.CYCLING,
            "MountainBikeRide": Sport.CYCLING,
            "GravelRide": Sport.CYCLING,
            "Swim": Sport.SWIMMING,
            "WeightTraining": Sport.STRENGTH,
            "Crossfit": Sport.STRENGTH,
            "Workout": Sport.STRENGTH,
            "Yoga": Sport.OTHER,
            "Hike": Sport.OTHER,
        }
        for strava_type, sport in expected.items():
            with self.subTest(strava_type=strava_type):
                self.assertIs(normalize_activity(activity(1, sport_type=strava_type)).sport, sport)

    def test_uses_type_when_sport_type_is_missing(self) -> None:
        record = activity(1, omit=("sport_type",), type="Swim")

        self.assertIs(normalize_activity(record).sport, Sport.SWIMMING)

    def test_sport_type_wins_over_type(self) -> None:
        record = activity(1, sport_type="TrailRun", type="Ride")

        self.assertIs(normalize_activity(record).sport, Sport.RUNNING)


class InstantsAndUnitsTest(unittest.TestCase):
    def test_full_mapping_of_a_valid_activity(self) -> None:
        """CA4: identificadores, deporte, instantes UTC, zona, duración y distancia."""
        result = normalize_activity(
            activity(
                12345678901,
                start_date="2026-10-20T11:00:00Z",
                elapsed_time=3725,
                moving_time=3600,
                distance=10000.5,
                total_elevation_gain=85.5,
                name="  Rodaje largo  ",
            )
        )

        self.assertEqual(result.activity_id, uuid5(NAMESPACE_URL, "strava:12345678901"))
        self.assertEqual(result.external_activity_id, "12345678901")
        self.assertEqual(result.started_at, datetime(2026, 10, 20, 11, 0, tzinfo=UTC))
        self.assertEqual(result.ended_at, datetime(2026, 10, 20, 12, 2, 5, tzinfo=UTC))
        self.assertEqual(result.time_zone, "America/Bogota")
        self.assertEqual((result.duration_seconds, result.moving_seconds), (3725, 3600))
        self.assertEqual(result.distance_meters, 10001)
        self.assertEqual(result.elevation_gain_meters, 86)
        self.assertEqual(result.name, "Rodaje largo")

    def test_start_with_offset_and_fraction_is_converted_to_utc_seconds(self) -> None:
        result = normalize_activity(activity(1, start_date="2026-10-20T06:00:00.750-05:00"))

        self.assertEqual(result.started_at, datetime(2026, 10, 20, 11, 0, tzinfo=UTC))

    def test_round_half_up_to_the_meter(self) -> None:
        cases = {0: 0, 0.4: 0, 0.5: 1, 2.5: 3, 1234.49: 1234, 1234.5: 1235, 10000: 10000}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(round_meters(value), expected)

    def test_optional_values_absent_or_unusable_become_null(self) -> None:
        result = normalize_activity(
            activity(1, omit=("moving_time", "total_elevation_gain", "name", "timezone"))
        )
        self.assertIsNone(result.moving_seconds)
        self.assertIsNone(result.elevation_gain_meters)
        self.assertIsNone(result.name)
        self.assertEqual(result.time_zone, "UTC")

        odd = normalize_activity(
            activity(2, moving_time="3600", total_elevation_gain=-3, name="   ")
        )
        self.assertIsNone(odd.moving_seconds)
        self.assertIsNone(odd.elevation_gain_meters)
        self.assertIsNone(odd.name)

    def test_name_is_trimmed_to_120_characters(self) -> None:
        result = normalize_activity(activity(1, name="x" * 200))

        self.assertEqual(result.name, "x" * 120)

    def test_time_zone_parsing(self) -> None:
        cases = {
            "(GMT-05:00) America/Bogota": "America/Bogota",
            "(GMT+01:00) Europe/Madrid": "Europe/Madrid",
            "America/Lima": "America/Lima",
            "(GMT+00:00) Mars/Olympus": "UTC",
            "(GMT-05:00)": "UTC",
            "": "UTC",
            "Hora local": "UTC",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(time_zone_from(raw), expected)
        self.assertEqual(time_zone_from(None), "UTC")


class InvalidActivityTest(unittest.TestCase):
    """CA7: una actividad incompleta o mal formada se omite."""

    def assert_skipped(self, record: object, reason: SkipReason, external: str | None) -> None:
        with self.assertRaises(InvalidActivity) as raised:
            normalize_activity(record)
        self.assertIs(raised.exception.reason, reason)
        self.assertEqual(raised.exception.external_activity_id, external)
        self.assertEqual(raised.exception.code, "invalid_activity")

    def test_missing_or_wrong_required_fields(self) -> None:
        cases: list[tuple[object, SkipReason, str | None]] = [
            ("no soy un objeto", SkipReason.NOT_AN_OBJECT, None),
            (activity(1, omit=("id",)), SkipReason.INVALID_ID, None),
            (activity(1, id="1"), SkipReason.INVALID_ID, None),
            (activity(1, id=True), SkipReason.INVALID_ID, None),
            (activity(1, id=0), SkipReason.INVALID_ID, None),
            (activity(7, omit=("start_date",)), SkipReason.INVALID_START_DATE, "7"),
            (activity(7, start_date="ayer"), SkipReason.INVALID_START_DATE, "7"),
            (activity(7, start_date="2026-10-20T11:00:00"), SkipReason.INVALID_START_DATE, "7"),
            (activity(7, omit=("elapsed_time",)), SkipReason.INVALID_ELAPSED_TIME, "7"),
            (activity(7, elapsed_time=0), SkipReason.INVALID_ELAPSED_TIME, "7"),
            (activity(7, elapsed_time=12.5), SkipReason.INVALID_ELAPSED_TIME, "7"),
            (activity(7, omit=("distance",)), SkipReason.INVALID_DISTANCE, "7"),
            (activity(7, distance=-1), SkipReason.INVALID_DISTANCE, "7"),
            (activity(7, distance="10"), SkipReason.INVALID_DISTANCE, "7"),
            (activity(7, omit=("sport_type", "type")), SkipReason.INVALID_TYPE, "7"),
            (activity(7, sport_type=3, type=None), SkipReason.INVALID_TYPE, "7"),
        ]
        for record, reason, external in cases:
            with self.subTest(reason=reason, record=str(record)[:40]):
                self.assert_skipped(record, reason, external)


class HashAndVersionTest(unittest.TestCase):
    def test_external_version_and_hash(self) -> None:
        result = normalize_activity(activity(99, elapsed_time=1800, distance=5000.25))

        expected = f"{uuid5(NAMESPACE_URL, 'strava:99')}:1800:5000.25"
        self.assertEqual(result.external_version, expected)
        self.assertEqual(result.payload_hash, hashlib.sha256(expected.encode()).hexdigest())

    def test_hash_ignores_name_but_changes_with_duration_or_distance(self) -> None:
        base = normalize_activity(activity(99))

        self.assertEqual(
            normalize_activity(activity(99, name="Otro")).payload_hash, base.payload_hash
        )
        self.assertNotEqual(
            normalize_activity(activity(99, elapsed_time=3601)).payload_hash, base.payload_hash
        )
        self.assertNotEqual(
            normalize_activity(activity(99, distance=10001.0)).payload_hash, base.payload_hash
        )

    def test_new_same_and_changed_versions(self) -> None:
        """CA5: nueva → 1; mismo hash → nada; hash distinto → versión siguiente."""
        user_id = uuid4()
        first = next_import(None, normalize_activity(activity(5)), user_id, NOW)
        assert first is not None
        self.assertEqual((first.version, first.imported_at, first.updated_at), (1, NOW, NOW))

        self.assertIsNone(next_import(first, normalize_activity(activity(5)), user_id, NOW))

        later = NOW + timedelta(hours=1)
        changed = next_import(
            first, normalize_activity(activity(5, distance=12000.0)), user_id, later
        )
        assert changed is not None
        self.assertEqual(changed.version, 2)
        self.assertEqual((changed.imported_at, changed.updated_at), (NOW, later))
        self.assertEqual(changed.activity_id, first.activity_id)


if __name__ == "__main__":
    unittest.main()
